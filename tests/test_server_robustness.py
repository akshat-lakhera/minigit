"""tests.test_server_robustness -- server, transport and remote-command robustness fixes."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
import zlib

from minigit import auth, errors, objects, pack, server
from minigit.objects import Commit, TreeEntry
from minigit.repo import Repository
from tests import helpers
from tests.test_server import SIG, ServerTestCase


HDR_END = bytes([13, 10, 13, 10])


def _raw_pack(entries: list[tuple[str, bytes]]) -> bytes:
    """A pack of (type, content) pairs that bypasses the store's own validation."""
    body = pack.MAGIC + struct.pack(">II", pack.VERSION, len(entries))
    for obj_type, content in entries:
        enc = objects.encode_object(obj_type, content)
        z = zlib.compress(enc)
        body += bytes.fromhex(hashlib.sha1(enc).hexdigest()) + struct.pack(">I", len(z)) + z
    return body + hashlib.sha1(body).digest()


class TestRobustness(ServerTestCase):
    def test_unsupported_methods_get_json_405(self):
        for method in ("OPTIONS", "PATCH", "TRACE", "FROBNICATE"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request(method, "/", headers={"X-Minigit-Protocol": "1"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            self.assertEqual(resp.status, 405, method)
            self.assertEqual(resp.getheader("Content-Type"), "application/json")
            self.assertEqual(body["code"], "method_not_allowed")

    def test_deeply_nested_json_is_a_400(self):
        deep = b"[" * 100000
        status, _, body = self.request("POST", "/proj/fetch", deep, content_type="application/json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "bad_request")
        frame = server.PUSH_MAGIC + struct.pack(">I", len(deep)) + deep
        status, _, body = self.request("POST", "/proj/push", frame, content_type=server.PUSH_CONTENT_TYPE)
        self.assertEqual(status, 400)

    def test_push_with_duplicate_tree_entry_names_is_rejected(self):
        blob = self.local.write("blob", b"x\n")
        sub = self.local.write("tree", objects.serialize_tree([TreeEntry("100644", "x", blob)]))
        raw = b"100644 d\0" + bytes.fromhex(blob) + b"40000 d\0" + bytes.fromhex(sub)
        with self.assertRaises(errors.ObjectError):
            objects.parse_tree(raw)
        with self.assertRaises(errors.ObjectError):
            self.local.write("tree", raw)
        tree_sha = hashlib.sha1(objects.encode_object("tree", raw)).hexdigest()
        commit = objects.serialize_commit(Commit(tree=tree_sha, parents=(), author=SIG, committer=SIG,
                                                 message="m\n"))
        csha = hashlib.sha1(objects.encode_object("commit", commit)).hexdigest()
        bad = _raw_pack([("blob", b"x\n"), ("tree", objects.serialize_tree(
            [TreeEntry("100644", "x", blob)])), ("tree", raw), ("commit", commit)])
        status, res = self.push([{"ref": "refs/heads/dup", "old": None, "new": csha}], bad)
        self.assertEqual(status, 422, res)
        self.assertEqual(res["code"], "bad_pack")
        self.assertIn("duplicate", res["error"])
        self.assertIsNone(self.server_repo().refs.resolve("refs/heads/dup"))

    def test_unsorted_tree_is_rejected(self):
        blob = self.local.write("blob", b"x\n")
        raw = b"100644 b\0" + bytes.fromhex(blob) + b"100644 a\0" + bytes.fromhex(blob)
        with self.assertRaises(errors.ObjectError):
            objects.parse_tree(raw)

    def test_fsck_flags_a_duplicate_tree(self):
        repo = self.server_repo()
        blob = repo.objects.write("blob", b"x\n")
        raw = b"100644 d\0" + bytes.fromhex(blob) + b"40000 d\0" + bytes.fromhex(blob)
        enc = objects.encode_object("tree", raw)
        dst = repo.objects.path(hashlib.sha1(enc).hexdigest())
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(zlib.compress(enc))
        problems = pack.fsck(repo)
        self.assertTrue(any("duplicate" in p for p in problems), problems)

    def test_ref_name_clash_is_reported_as_a_clash(self):
        base = self.push_initial()
        status, res = self.push([{"ref": "refs/heads/main/x", "old": None, "new": base}], b"")
        self.assertEqual(status, 409, res)
        r = res["results"][0]
        self.assertEqual(r["reason"], "ref-clash")
        self.assertIn("clashes", r["message"])
        status, res = self.push([{"ref": "refs/heads/Main", "old": None, "new": base}], b"")
        self.assertEqual(res["results"][0]["reason"], "ref-clash")


class TestCliArgs(unittest.TestCase):
    def run_cli(self, *args, cwd):
        return helpers.run(list(args), cwd=cwd, env=helpers.make_env(cwd), check=False)

    def test_out_of_range_port_is_a_usage_error(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.run_cli("server", "init", "R", cwd=tmp)
            for port in ("70000", "-1", "65536", "abc"):
                res = self.run_cli("serve", "--root", "R", "--port", port, "--quiet", cwd=tmp)
                self.assertEqual(res.returncode, 2, res)
                self.assertNotIn("internal error", res.err)
                self.assertIn("port", res.err)

    def test_root_that_is_a_file_names_the_root(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with open(os.path.join(tmp, "R"), "w") as fh:
                fh.write("x")
            for extra in ((), ("--init",)):
                res = self.run_cli("serve", "--root", "R", "--port", "0", "--quiet", *extra, cwd=tmp)
                self.assertEqual(res.returncode, 1, res)
                self.assertIn("not a directory", res.err)
                self.assertNotIn("listen", res.err)

    def test_missing_root_is_not_created_without_init(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            res = self.run_cli("serve", "--root", "nonexist", "--port", "0", "--quiet", cwd=tmp)
            self.assertEqual(res.returncode, 1, res)
            self.assertIn("not a minigit server root", res.err)
            self.assertFalse(os.path.exists(os.path.join(tmp, "nonexist")))

    def test_empty_acl_means_nobody(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            self.run_cli("server", "init", "R", cwd=tmp)
            res = self.run_cli("server", "create-repo", "R", "locked", "--readers", "", "--writers", "", cwd=tmp)
            self.assertEqual(res.returncode, 0, res)
            with open(os.path.join(tmp, "R", "server.json")) as fh:
                rec = json.load(fh)["repos"]["locked"]
            self.assertEqual((rec["readers"], rec["writers"]), ([], []))
            cfg = auth.ServerConfig(os.path.join(tmp, "R"))
            self.assertFalse(cfg.can_read("alice", "locked"))
            self.assertFalse(cfg.can_write("alice", "locked"))

    def test_init_on_a_file_is_a_clean_error(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            with open(os.path.join(tmp, "afile"), "w") as fh:
                fh.write("x")
            for target in ("afile", os.path.join("afile", "sub")):
                res = self.run_cli("init", target, cwd=tmp)
                self.assertEqual(res.returncode, 1, res)
                self.assertTrue(res.err.startswith("error: "), res.err)
                self.assertNotIn("internal error", res.err)


class TestCliCollab(unittest.TestCase):
    """End-to-end through the real CLI against a live server process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.env = helpers.make_env(self.tmp)
        self.root = os.path.join(self.tmp, "srv")
        auth.server_init(self.root)
        self.token = auth.add_user(self.root, "alice")
        auth.create_repo(self.root, "proj")
        auth.create_repo(self.root, "empty")
        self.srv = helpers.ServerProcess(self.root, env=self.env).start()
        self.addCleanup(self.srv.stop)
        self.env = helpers.make_env(self.tmp, "alice", self.token)
        self.work = os.path.join(self.tmp, "w")
        os.makedirs(self.work)

    def cli(self, *args, cwd=None, check=True, env=None):
        return helpers.run(list(args), cwd=cwd or self.work, env=env or self.env, check=check)

    def clone_with_commit(self, name="A", repo="proj"):
        self.cli("clone", f"{self.srv.url}/{repo}", name)
        d = os.path.join(self.work, name)
        self.cli("config", "user.name", "A", cwd=d)
        self.cli("config", "user.email", "a@x", cwd=d)
        helpers.write(os.path.join(d, "a.txt"), "1\n")
        self.cli("add", ".", cwd=d)
        self.cli("commit", "-m", "c1", cwd=d)
        return d

    @unittest.skipUnless(os.name == "nt", "refs.update refuses device names on Windows only")
    def test_push_to_a_windows_device_name_is_rejected_cleanly(self):
        # QA bug 4: 'push origin main:CON' was "internal server error" (HTTP 500 + traceback in the log).
        d = self.clone_with_commit()
        self.cli("push", "-u", "origin", "main", cwd=d)
        for dst in ("CON", "aux.txt"):
            # The CLI now refuses the name before contacting the server (QA bug 5): exit 2, no request.
            res = self.cli("push", "origin", f"main:{dst}", cwd=d, check=False)
            self.assertEqual(res.returncode, 2, res)
            self.assertIn("reserved device name", res.err, res)
            self.assertNotIn("internal server error", res.err, res)
        # A client without that check still gets a clean 409 from the server: covered at the HTTP level by
        # tests.test_server (test_windows_reserved_branch_name_is_a_clean_rejection).
        self.assertNotIn("Traceback", self.srv.log())
        self.assertNotIn(" 500 ", self.srv.log())
        bare = Repository.open(os.path.join(self.root, "repos", "proj"))
        self.assertEqual(sorted(bare.refs.list()), ["refs/heads/main"])

    def test_push_delete_accepts_git_option_order(self):
        d = self.clone_with_commit()
        self.cli("push", "-u", "origin", "main", cwd=d)
        for name, order in (("f1", ("origin", "--delete", "f1")), ("f2", ("--delete", "origin", "f2"))):
            self.cli("branch", name, cwd=d)
            self.cli("push", "origin", name, cwd=d)
            res = self.cli("push", *order, cwd=d)
            self.assertIn("[deleted]", res.out, res)
        self.cli("branch", "g", cwd=d)
        self.cli("push", "origin", "g", cwd=d)
        res = self.cli("push", "origin", "g", "--delete", cwd=d)
        self.assertIn("[deleted]", res.out)

    def test_empty_clone_tracks_origin_after_first_push(self):
        d = self.clone_with_commit("E", "empty")
        self.cli("push", "origin", "main", cwd=d)
        with open(os.path.join(d, ".minigit", "config")) as fh:
            cfg = fh.read()
        self.assertIn('[branch "main"]', cfg)
        self.assertIn("refs/heads/main", cfg)
        res = self.cli("status", cwd=d)
        self.assertIn("origin/main", res.out)

    def test_pull_missing_branch_hint(self):
        d = self.clone_with_commit()
        self.cli("push", "-u", "origin", "main", cwd=d)
        res = self.cli("pull", "origin", "nope", cwd=d, check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("couldn't find remote ref 'nope'", res.err)
        self.assertNotIn("-u", res.err)

    def test_clash_is_reported_as_clash_not_stale(self):
        d = self.clone_with_commit()
        self.cli("push", "-u", "origin", "main", cwd=d)
        res = self.cli("push", "origin", "main:main/x", cwd=d, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("clash", res.err)
        self.assertNotIn("stale", res.err)
        self.assertNotIn("pull", res.err.replace("pulling will not resolve", ""))

    def test_clone_url_without_repo_path(self):
        for url in (self.srv.url, self.srv.url + "/"):
            res = self.cli("clone", url, "dest", check=False)
            self.assertEqual(res.returncode, 1, res)
            self.assertIn("no repository path", res.err)
            self.assertFalse(os.path.exists(os.path.join(self.work, "dest")))

    def test_no_credentials_message(self):
        env = helpers.make_env(self.tmp)
        res = self.cli("clone", f"{self.srv.url}/proj", "anon", check=False, env=env)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("authentication required", res.err)
        self.assertIn("no credentials", res.err)
        self.assertIn("login", res.err)
        bad = helpers.make_env(self.tmp, "alice", "wrong")
        res = self.cli("clone", f"{self.srv.url}/proj", "anon", check=False, env=bad)
        self.assertIn("authentication failed", res.err)

    def test_private_repo_is_denied_not_missing(self):
        auth.add_user(self.root, "bob")
        auth.create_repo(self.root, "secret", readers=["bob"], writers=["bob"])
        a = self.cli("clone", f"{self.srv.url}/secret", "s1", check=False)
        b = self.cli("clone", f"{self.srv.url}/nothere", "s2", check=False)
        self.assertEqual(a.returncode, 1)
        self.assertIn("permission denied", a.err)
        self.assertEqual(b.returncode, 1)
        self.assertIn("not found", b.err)


class _CutProxy(threading.Thread):
    """TCP proxy that forwards the request and cuts the response in half."""

    def __init__(self, target_port: int, cut_after: int | None = None):
        super().__init__(daemon=True)
        self.target_port = target_port
        self.cut_after = cut_after
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def run(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            with client:
                up = socket.create_connection(("127.0.0.1", self.target_port))
                with up:
                    client.settimeout(5)
                    data = b""
                    try:
                        while b"\r\n\r\n" not in data:
                            data += client.recv(65536)
                        head, _, rest = data.partition(b"\r\n\r\n")
                        length = 0
                        for line in head.split(b"\r\n"):
                            if line.lower().startswith(b"content-length:"):
                                length = int(line.split(b":")[1])
                        while len(rest) < length:
                            rest += client.recv(65536)
                    except OSError:
                        continue
                    up.sendall(head + HDR_END + rest)
                    resp = b""
                    while True:
                        chunk = up.recv(65536)
                        if not chunk:
                            break
                        resp += chunk
                    if self.cut_after is None:
                        start = resp.find(HDR_END) + len(HDR_END)
                        client.sendall(resp[: start + (len(resp) - start) // 2])
                    else:
                        client.sendall(resp[: self.cut_after])

    def close(self):
        self.sock.close()


class TestTruncatedTransfer(unittest.TestCase):
    def test_cut_off_response_is_reported_as_interrupted(self):
        from minigit.transport import HttpRemote
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = os.path.join(tmp, "srv")
            auth.server_init(root)
            auth.create_repo(root, "proj", public=True)
            httpd = server.make_server(root, "127.0.0.1", 0)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            proxy = _CutProxy(httpd.server_address[1])
            proxy.start()
            try:
                rem = HttpRemote(f"http://127.0.0.1:{proxy.port}/proj")
                with self.assertRaises(errors.RemoteError) as cm:
                    rem.info_refs()
                msg = str(cm.exception)
                self.assertIn("transfer interrupted", msg)
                self.assertNotIn("could not connect", msg)
            finally:
                proxy.close()
                httpd.shutdown()
                httpd.server_close()
                t.join(5)


if __name__ == "__main__":
    unittest.main()

"""tests.test_remote -- owner: M6 client.

Unit tests for minigit.transport and minigit.remote.  The transport tests run
against a tiny fake HTTP server defined here; the collaboration tests run the
real protocol against an in-process ``server.make_server`` on localhost.
"""
from __future__ import annotations

import base64
import contextlib
import http.server
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

from minigit import auth, errors, index, objects, pack, server, trees
from minigit import remote as R
from minigit import transport as T
from minigit.repo import Repository

IDENTITY_ENV = {
    "MINIGIT_AUTHOR_NAME": "Test User",
    "MINIGIT_AUTHOR_EMAIL": "test@example.com",
    "MINIGIT_AUTHOR_DATE": "1700000000 +0000",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _EnvCase(unittest.TestCase):
    """Temp dir + isolated environment (credentials, global config, identity)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tmp.name
        env = {k: v for k, v in os.environ.items()
               if k not in ("MINIGIT_USER", "MINIGIT_TOKEN", "HTTP_PROXY", "http_proxy")}
        env.update(IDENTITY_ENV)
        env["MINIGIT_CREDENTIALS"] = os.path.join(self.tmp, "credentials.json")
        env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(self.tmp, "globalconfig")
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


# ===================================================================== URLs

class SplitUrlTests(unittest.TestCase):
    def test_no_userinfo(self):
        self.assertEqual(T.split_url("http://h:1/proj"), ("http://h:1/proj", None))

    def test_userinfo_is_stripped(self):
        self.assertEqual(T.split_url("http://alice:TOK@127.0.0.1:8765/proj"),
                         ("http://127.0.0.1:8765/proj", ("alice", "TOK")))

    def test_percent_encoded_token(self):
        self.assertEqual(T.split_url("http://a%40b:t%3Ak@h:1/p"), ("http://h:1/p", ("a@b", "t:k")))

    def test_user_without_token(self):
        self.assertEqual(T.split_url("http://alice@h:1/p"), ("http://h:1/p", ("alice", "")))

    def test_trailing_slash_kept_by_split(self):
        self.assertEqual(T.split_url("http://h:1/p/")[0], "http://h:1/p/")

    def test_check_url_rejects_non_http(self):
        for bad in ("ftp://h/p", "proj", "http://", "/tmp/repo", "http://h:notaport/p"):
            with self.subTest(bad=bad):
                with self.assertRaises(errors.RemoteError):
                    T.check_url(bad)

    def test_redact_url_masks_only_the_token(self):
        self.assertEqual(T.redact_url("http://alice:TOK@h:1/p"), "http://alice:***@h:1/p")
        self.assertEqual(T.redact_url("http://alice@h:1/p"), "http://alice@h:1/p")
        self.assertEqual(T.redact_url("http://h:1/p?a@b"), "http://h:1/p?a@b")
        self.assertEqual(T.redact_url("proj"), "proj")

    def test_errors_about_a_bad_url_never_show_the_token(self):
        for bad in ("http://alice:SECRET@h:99999/p", "http://alice:SECRET@[::1/p"):
            with self.subTest(bad=bad):
                with self.assertRaises(errors.MinigitError) as cm:
                    T.split_url(bad)
                self.assertNotIn("SECRET", cm.exception.message)
                self.assertIn("alice:***@", cm.exception.message)
        with self.assertRaises(errors.RemoteError) as cm:
            T.check_url("ftp://alice:SECRET@h/p")
        self.assertNotIn("SECRET", cm.exception.message)

    def test_local_paths_and_ssh_urls_get_a_hint(self):
        for bad in ("/tmp/repo", "../repo", "git@github.com:x/y.git"):
            with self.subTest(bad=bad):
                with self.assertRaises(errors.RemoteError) as cm:
                    T.check_url(bad)
                self.assertIn("not supported", cm.exception.hint)

    def test_credential_key(self):
        self.assertEqual(T.credential_key("http://Alice:x@LocalHost:8765/proj/"), "http://localhost:8765")
        self.assertEqual(T.credential_key("http://h/p"), "http://h:80")
        self.assertEqual(T.credential_key("https://h/p"), "https://h:443")


# ============================================================== credentials

class CredentialTests(_EnvCase):
    URL = "http://127.0.0.1:8765/proj"

    def test_credentials_path_env_and_default(self):
        self.assertEqual(T.credentials_path(), os.environ["MINIGIT_CREDENTIALS"])
        del os.environ["MINIGIT_CREDENTIALS"]
        self.assertEqual(T.credentials_path(),
                         os.path.join(os.path.expanduser("~"), ".minigit-credentials.json"))

    def test_none_when_nothing_configured(self):
        self.assertIsNone(T.load_credentials(self.URL))

    def test_save_and_load_file(self):
        T.save_credentials(self.URL, "alice", "TOK", host_default=True)
        with open(T.credentials_path(), "rb") as fh:
            data = json.loads(fh.read())
        entry = {"user": "alice", "token": "TOK"}
        self.assertEqual(data, {"http://127.0.0.1:8765": entry, "http://127.0.0.1:8765|alice": entry})
        self.assertEqual(T.load_credentials("http://127.0.0.1:8765/other"), ("alice", "TOK"))
        self.assertIsNone(T.load_credentials("http://127.0.0.1:9999/proj"))
        self.assertFalse(os.path.exists(T.credentials_path() + ".lock"))

    def test_save_keeps_other_hosts_and_overwrites_same_host(self):
        T.save_credentials(self.URL, "alice", "1", host_default=True)
        T.save_credentials("http://other:1/p", "bob", "2", host_default=True)
        T.save_credentials(self.URL, "carol", "3", host_default=True)  # a second `login` may switch
        self.assertEqual(T.load_credentials(self.URL), ("carol", "3"))
        self.assertEqual(T.load_credentials("http://other:1/x"), ("bob", "2"))

    def test_userinfo_credentials_do_not_change_the_host_default(self):
        # QA bug 1: a `user:token@` URL used once (clone / remote add) must not change who
        # every other clone of that server acts as; only `login` sets the host default.
        T.save_credentials(self.URL, "alice", "A", host_default=True)   # minigit login
        T.save_credentials(self.URL, "bob", "B")                         # clone http://bob:B@...
        with open(T.credentials_path(), "rb") as fh:
            data = json.loads(fh.read())
        self.assertEqual(data["http://127.0.0.1:8765"], {"user": "alice", "token": "A"})
        self.assertEqual(T.load_credentials(self.URL), ("alice", "A"))
        self.assertEqual(T.load_credentials("http://bob@127.0.0.1:8765/proj"), ("bob", "B"))
        T.save_credentials("http://fresh:1/p", "carol", "C")  # no login for that host at all
        self.assertIsNone(T.load_credentials("http://fresh:1/p"))
        self.assertEqual(T.load_credentials("http://carol@fresh:1/p"), ("carol", "C"))

    def test_order_env_then_userinfo_then_file(self):
        T.save_credentials(self.URL, "filer", "F", host_default=True)
        with_info = "http://urler:U@127.0.0.1:8765/proj"
        self.assertEqual(T.load_credentials(self.URL), ("filer", "F"))
        self.assertEqual(T.load_credentials(with_info), ("urler", "U"))
        os.environ["MINIGIT_USER"] = "enver"
        self.assertEqual(T.load_credentials(with_info), ("urler", "U"), "env needs user AND token")
        os.environ["MINIGIT_TOKEN"] = "E"
        self.assertEqual(T.load_credentials(with_info), ("enver", "E"))
        self.assertEqual(T.load_credentials(self.URL), ("enver", "E"))

    def test_corrupt_file_is_a_clear_error(self):
        with open(T.credentials_path(), "wb") as fh:
            fh.write(b"{not json")
        with self.assertRaises(errors.MinigitError) as cm:
            T.load_credentials(self.URL)
        self.assertIn("corrupt", str(cm.exception))

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_file_mode_600(self):
        T.save_credentials(self.URL, "alice", "TOK")
        self.assertEqual(os.stat(T.credentials_path()).st_mode & 0o777, 0o600)

    def test_login_writes_credentials(self):
        R.login("http://127.0.0.1:8765/proj", "alice", "TOK")
        self.assertEqual(T.load_credentials(self.URL), ("alice", "TOK"))
        with self.assertRaises(errors.MinigitError):
            R.login(self.URL, "alice", "")
        with self.assertRaises(errors.MinigitError):
            R.login("not-a-url", "alice", "TOK")

    def test_http_remote_strips_userinfo_and_trailing_slash(self):
        rem = T.HttpRemote("http://alice:TOK@127.0.0.1:8765/proj/")
        self.assertEqual(rem.url, "http://127.0.0.1:8765/proj")
        self.assertEqual(rem.credentials, ("alice", "TOK"))
        self.assertNotIn("TOK", rem.url)


# ==================================================== transport vs fake HTTP

class _FakeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # silence
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append({"method": self.command, "path": self.path,
                                     "headers": {k.lower(): v for k, v in self.headers.items()},
                                     "body": body})
        status, ctype, payload, *extra = self.server.responder(self.command, self.path, body)
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra[0] if extra else {}).items():
            self.send_header(key, value)
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="minigit"')
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _handle


class TransportFakeServerTests(_EnvCase):
    def setUp(self):
        super().setUp()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
        self.httpd.daemon_threads = True
        self.httpd.requests = []
        self.httpd.responder = lambda m, p, b: (200, "application/json", {})
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.url = self.base + "/proj"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)
        super().tearDown()

    def respond(self, status, payload, ctype="application/json"):
        self.httpd.responder = lambda m, p, b: (status, ctype, payload)

    def test_info_refs_headers_and_parse(self):
        sha = "a" * 40
        self.respond(200, {"protocol": 1, "head": "refs/heads/main", "refs": {"refs/heads/main": sha}})
        rem = T.HttpRemote(f"http://alice:s3cret@127.0.0.1:{self.httpd.server_address[1]}/proj/")
        info = rem.info_refs()
        self.assertEqual(info, T.RemoteRefs(head="refs/heads/main", refs={"refs/heads/main": sha}))
        req = self.httpd.requests[-1]
        self.assertEqual((req["method"], req["path"]), ("GET", "/proj/info/refs"))
        self.assertEqual(req["headers"]["x-minigit-protocol"], "1")
        self.assertEqual(req["headers"]["authorization"],
                         "Basic " + base64.b64encode(b"alice:s3cret").decode())

    def test_no_authorization_header_without_credentials(self):
        self.respond(200, {"protocol": 1, "head": None, "refs": {}})
        info = T.HttpRemote(self.url).info_refs()
        self.assertEqual(info, T.RemoteRefs(head=None, refs={}))
        self.assertNotIn("authorization", self.httpd.requests[-1]["headers"])

    def test_proxy_environment_is_ignored(self):
        os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = "http://127.0.0.1:1"
        self.respond(200, {"protocol": 1, "head": None, "refs": {}})
        self.assertEqual(T.HttpRemote(self.url).info_refs().refs, {})

    def test_malformed_info_refs(self):
        for payload in ({"protocol": 1, "refs": {"refs/heads/main": "xyz"}},
                        {"protocol": 1, "refs": {"refs/../x": "a" * 40}},
                        {"protocol": 2, "refs": {}}, [1, 2]):
            with self.subTest(payload=payload):
                self.respond(200, payload)
                with self.assertRaises(errors.RemoteError):
                    T.HttpRemote(self.url).info_refs()
        self.respond(200, b"<html>", "text/html")
        with self.assertRaises(errors.RemoteError):
            T.HttpRemote(self.url).info_refs()

    def test_401_is_auth_error(self):
        self.respond(401, {"error": "authentication required", "code": "auth"})
        with self.assertRaises(errors.AuthError) as cm:
            T.HttpRemote(self.url).info_refs()
        self.assertEqual(str(cm.exception),
                         f"authentication required for '{self.url}' (no credentials configured)")
        self.assertEqual(cm.exception.status, 401)
        with mock.patch.dict(os.environ, {"MINIGIT_USER": "alice", "MINIGIT_TOKEN": "bad"}):
            with self.assertRaises(errors.AuthError) as cm:
                T.HttpRemote(self.url).info_refs()
        self.assertEqual(str(cm.exception),
                         f"authentication failed for '{self.url}': check user/token (minigit login)")

    def test_403_is_permission_denied(self):
        self.respond(403, {"error": "user 'bob' may not write to 'proj'", "code": "forbidden"})
        with self.assertRaises(errors.PermissionDeniedError) as cm:
            T.HttpRemote(self.url).push([], b"")
        self.assertTrue(str(cm.exception).startswith("permission denied: "))
        self.assertIn("bob", str(cm.exception))
        self.respond(403, {"error": "permission denied: nope", "code": "forbidden"})
        with self.assertRaises(errors.PermissionDeniedError) as cm:
            T.HttpRemote(self.url).info_refs()
        self.assertEqual(str(cm.exception), "permission denied: nope")

    def test_other_status_uses_body_error(self):
        self.respond(404, {"error": "repository 'proj' not found", "code": "not_found"})
        with self.assertRaises(errors.RemoteError) as cm:
            T.HttpRemote(self.url).info_refs()
        self.assertEqual(str(cm.exception), "repository 'proj' not found")
        self.assertEqual((cm.exception.status, cm.exception.code), (404, "not_found"))
        self.respond(500, b"boom", "text/plain")
        with self.assertRaises(errors.RemoteError) as cm:
            T.HttpRemote(self.url).info_refs()
        self.assertIn("500", str(cm.exception))

    def test_redirects_are_not_followed(self):
        # Following a redirect would send the Authorization header to another host.
        target = self.base + "/elsewhere/info/refs"
        self.httpd.responder = lambda m, p, b: (
            (302, "text/plain", b"moved", {"Location": target}) if p.startswith("/proj/")
            else (200, "application/json", {"protocol": 1, "head": None, "refs": {}}))
        rem = T.HttpRemote(f"http://alice:s3cret@127.0.0.1:{self.httpd.server_address[1]}/proj")
        with self.assertRaises(errors.RemoteError) as cm:
            rem.info_refs()
        self.assertIn("302", str(cm.exception))
        self.assertEqual([r["path"] for r in self.httpd.requests], ["/proj/info/refs"])

    def test_connection_refused(self):
        url = f"http://127.0.0.1:{_free_port()}/proj"
        with self.assertRaises(errors.RemoteError) as cm:
            T.HttpRemote(url, timeout=5).info_refs()
        self.assertTrue(str(cm.exception).startswith(f"could not connect to server at {url}: "))
        # a readable reason instead of the raw socket error (e.g. "[WinError 10061] ...")
        self.assertEqual(str(cm.exception), f"could not connect to server at {url}: connection refused")
        self.assertIn("minigit serve", cm.exception.hint)

    def test_unknown_host_is_named(self):
        url = "http://no-such-host.invalid:8765/proj"
        with self.assertRaises(errors.RemoteError) as cm:
            T.HttpRemote(url, timeout=5).info_refs()
        self.assertEqual(str(cm.exception),
                         f"could not connect to server at {url}: could not resolve host 'no-such-host.invalid'")

    def test_not_a_minigit_server_gets_a_hint(self):
        self.respond(200, b"<html>hello</html>", "text/html")
        with self.assertRaises(errors.RemoteError) as cm:
            T.HttpRemote(self.url).info_refs()
        self.assertIn("(not JSON)", str(cm.exception))
        self.assertIn("'minigit serve' server", cm.exception.hint)

    def test_slow_push_uses_the_long_data_timeout(self):
        # QA bug 2: a big push legitimately takes minutes on the server (one fsync per object);
        # only the small requests are bound by `timeout`, push/fetch wait `data_timeout`.
        def responder(method, path, body):
            if path.endswith(("/push", "/fetch")):
                time.sleep(1.0)
            return 200, "application/json", {"ok": True, "results": []}

        self.httpd.responder = responder
        rem = T.HttpRemote(self.url, timeout=0.3, data_timeout=10)
        self.assertEqual(rem.push([], b""), {"ok": True, "results": []})
        self.assertEqual(rem.fetch_pack(["a" * 40], []), b'{"ok": true, "results": []}')

    def test_push_timeout_is_reported_as_a_timeout_not_a_connect_failure(self):
        def responder(method, path, body):
            time.sleep(2.0)
            return 200, "application/json", {"ok": True, "results": []}

        self.httpd.responder = responder
        self.httpd.handle_error = lambda request, address: None  # the late reply hits a closed socket
        rem = T.HttpRemote(self.url, timeout=5, data_timeout=0.3)
        with self.assertRaises(errors.RemoteError) as cm:
            rem.push([{"ref": "refs/heads/main", "old": None, "new": "a" * 40}], b"")
        self.assertEqual(str(cm.exception), f"timed out waiting for the server at {self.url}; the push may "
                                            "still be applied, run 'minigit fetch' to check")
        self.assertIn("MINIGIT_HTTP_TIMEOUT", cm.exception.hint)
        with self.assertRaises(errors.RemoteError) as cm:
            rem.fetch_pack(["a" * 40], [])
        self.assertTrue(str(cm.exception).startswith(f"timed out waiting for the server at {self.url}; "))
        with self.assertRaises(errors.RemoteError) as cm:
            T.HttpRemote(self.url, timeout=0.3).info_refs()
        self.assertTrue(str(cm.exception).startswith("timed out waiting"), str(cm.exception))

    def test_data_timeout_from_environment(self):
        self.assertEqual(T.HttpRemote(self.url).data_timeout, T.DATA_TIMEOUT)
        self.assertEqual(T.HttpRemote(self.url).connect_timeout, T.CONNECT_TIMEOUT)
        os.environ["MINIGIT_HTTP_TIMEOUT"] = "1800"
        self.assertEqual(T.HttpRemote(self.url).data_timeout, 1800.0)
        self.assertEqual(T.HttpRemote(self.url, data_timeout=7).data_timeout, 7)
        for bad in ("soon", "0", "-5", "inf", "nan"):
            os.environ["MINIGIT_HTTP_TIMEOUT"] = bad
            with self.subTest(bad=bad), self.assertRaises(errors.MinigitError) as cm:
                T.HttpRemote(self.url)
            self.assertIn("MINIGIT_HTTP_TIMEOUT", str(cm.exception))

    def test_fetch_pack_request(self):
        self.respond(200, b"PACKBYTES", "application/x-minigit-pack")
        data = T.HttpRemote(self.url).fetch_pack(["a" * 40], ["b" * 40])
        self.assertEqual(data, b"PACKBYTES")
        req = self.httpd.requests[-1]
        self.assertEqual((req["method"], req["path"]), ("POST", "/proj/fetch"))
        self.assertEqual(json.loads(req["body"]), {"want": ["a" * 40], "have": ["b" * 40]})
        self.assertEqual(req["headers"]["content-length"], str(len(req["body"])))

    def test_push_framing_and_ok(self):
        result = {"ok": True, "results": [{"ref": "refs/heads/main", "status": "ok"}]}
        self.respond(200, result)
        upd = [{"ref": "refs/heads/main", "old": None, "new": "c" * 40}]
        self.assertEqual(T.HttpRemote(self.url).push(upd, b"PACK", force=True), result)
        req = self.httpd.requests[-1]
        self.assertEqual(req["path"], "/proj/push")
        self.assertEqual(req["headers"]["content-type"], "application/x-minigit-push")
        body = req["body"]
        self.assertTrue(body.startswith(b"MGPUSH1\n"))
        (n,) = struct.unpack(">I", body[8:12])
        meta = json.loads(body[12:12 + n])
        self.assertEqual(meta, {"protocol": 1, "force": True, "updates": upd})
        self.assertEqual(body[12 + n:], b"PACK")

    def test_push_409_raises_push_rejected(self):
        results = [{"ref": "refs/heads/main", "status": "rejected", "reason": "stale",
                    "current": "d" * 40, "message": "remote ref changed"}]
        self.respond(409, {"ok": False, "code": "rejected", "error": "push rejected", "results": results})
        with self.assertRaises(errors.PushRejectedError) as cm:
            T.HttpRemote(self.url).push([], b"")
        self.assertEqual(cm.exception.results, results)
        self.assertEqual(cm.exception.url, self.url)
        self.assertEqual(cm.exception.status, 409)

    def test_list_repos(self):
        self.respond(200, {"repos": ["b", "a"]})
        self.assertEqual(T.HttpRemote(self.url).list_repos(), ["a", "b"])
        self.assertEqual(self.httpd.requests[-1]["path"], "/")


# ========================================================== format helpers

class FormatTests(unittest.TestCase):
    A, B = "a1b2c3d" + "0" * 33, "e4f5a6b" + "0" * 33

    def test_format_fetch_lines(self):
        changes = [
            R.RefChange("refs/remotes/origin/main", self.A, self.B, "updated"),
            R.RefChange("refs/remotes/origin/hot", self.A, self.B, "forced"),
            R.RefChange("refs/remotes/origin/feature", None, self.B, "new"),
            R.RefChange("refs/remotes/origin/old", self.A, None, "deleted"),
            R.RefChange("refs/remotes/origin/same", self.A, self.A, "up-to-date"),
        ]
        self.assertEqual(R.format_fetch("http://h:1/p", changes).split("\n"), [
            "From http://h:1/p",
            "   a1b2c3d..e4f5a6b  main -> origin/main",
            " + a1b2c3d...e4f5a6b hot -> origin/hot (forced update)",
            " * [new branch]      feature -> origin/feature",
            " - [deleted]         old -> origin/old",
        ])
        self.assertEqual(R.format_fetch("u", [changes[-1]]), "")
        self.assertEqual(R.format_fetch("u", []), "")

    def test_format_push_rejection(self):
        # QA bug 11: a lost race ("stale") reads "fetch first" (git keeps "stale info" for
        # --force-with-lease); a non-fast-forward says the branch is behind.
        behind = "hint: Updates were rejected because the tip of your branch is behind its remote counterpart."
        missing = "hint: Updates were rejected because the remote contains work that you do not have locally."
        for reason, text, first_hint in (("non-fast-forward", "non-fast-forward", behind),
                                         ("fetch-first", "fetch first", missing),
                                         ("stale", "fetch first", missing)):
            err = errors.PushRejectedError("push rejected", url="http://h:1/p", results=[
                {"ref": "refs/heads/main", "status": "rejected", "reason": reason}])
            self.assertEqual(R.format_push_rejection(err, "main", "main"), "\n".join([
                "To http://h:1/p",
                f" ! [rejected]        main -> main ({text})",
                "error: failed to push some refs to 'http://h:1/p'",
                first_hint,
                "hint: Integrate the remote changes first ('minigit pull'), then push again.",
            ]))

    def test_format_force_disabled_rejection_does_not_say_pull(self):
        # QA bug 11: pulling cannot help when the repository refuses every forced update
        err = errors.PushRejectedError("push rejected", url="http://h:1/p", results=[
            {"ref": "refs/heads/main", "status": "rejected", "reason": "force-disabled"}])
        self.assertEqual(R.format_push_rejection(err, "main", "main"), "\n".join([
            "To http://h:1/p",
            " ! [rejected]        main -> main (forced update disabled)",
            "error: failed to push some refs to 'http://h:1/p'",
            "hint: this repository does not allow forced pushes (ask the admin for --allow-force or push a "
            "new branch)",
        ]))

    def test_guess_dir(self):
        self.assertEqual(R._guess_dir("http://h:1/proj"), "proj")
        self.assertEqual(R._guess_dir("http://h:1/a/proj.git/"), "proj")
        with self.assertRaises(errors.UsageError):
            R._guess_dir("http://h:1/")


# ============================================ live in-process server tests

class LiveServerCase(_EnvCase):
    allow_force = False

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "srv")
        auth.server_init(self.root)
        self.tokens = {name: auth.add_user(self.root, name) for name in ("alice", "bob", "reader")}
        auth.create_repo(self.root, "proj", readers=("*",), writers=("alice", "bob"),
                         allow_force=self.allow_force)
        self.httpd = server.make_server(self.root, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.url = self.base + "/proj"
        self.server_repo = Repository.open(os.path.join(self.root, "repos", "proj"))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)
        super().tearDown()

    # -- helpers -------------------------------------------------------------
    @contextlib.contextmanager
    def as_user(self, name, token=None):
        with mock.patch.dict(os.environ, {"MINIGIT_USER": name,
                                          "MINIGIT_TOKEN": token or self.tokens[name]}):
            yield

    def clone(self, who, dirname=None, url=None):
        with self.as_user(who):
            repo, msgs = R.clone(url or self.url, dirname or who, self.tmp)
        return repo, msgs

    def commit(self, repo, files, message):
        """Write files (bytes, or None to delete), stage them, and commit on HEAD."""
        store = repo.objects
        with index.locked_index(repo) as idx:
            for path, data in files.items():
                full = repo.abspath(path)
                if data is None:
                    if os.path.exists(full):
                        os.unlink(full)
                    idx.remove(path)
                    continue
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as fh:
                    fh.write(data)
                sha = store.write("blob", data)
                idx.put(index.IndexEntry(path=path, mode="100644", sha=sha, size=len(data), mtime_ns=0))
            flat = idx.flat()
        tree = trees.build_tree(store, flat)
        head = repo.head_commit()
        sig = repo.identity()
        c = objects.Commit(tree=tree, parents=(head,) if head else (), author=sig, committer=sig,
                           message=message.rstrip() + "\n")
        sha = store.write("commit", objects.serialize_commit(c))
        repo.refs.update_head(sha, expected=head)
        return sha

    def push(self, who, repo, *args, **kw):
        with self.as_user(who):
            return R.push(repo, *args, **kw)

    def fetch(self, who, repo, remote="origin"):
        with self.as_user(who):
            return R.fetch(repo, remote)

    def pull(self, who, repo, *args):
        with self.as_user(who):
            return R.pull(repo, *args)

    def server_ref(self, name="refs/heads/main"):
        return self.server_repo.refs.resolve(name)

    def read(self, repo, path):
        with open(repo.abspath(path), "rb") as fh:
            return fh.read()

    def alice_with_commit(self, files=None, message="first"):
        alice, _ = self.clone("alice")
        sha = self.commit(alice, files or {"a.txt": b"alpha\n", "dir/b.txt": b"beta\n"}, message)
        self.push("alice", alice, set_upstream=True)
        return alice, sha


class CloneTests(LiveServerCase):
    def test_clone_empty_repository(self):
        repo, msgs = self.clone("alice")
        self.assertIn("warning: You appear to have cloned an empty repository.", msgs)
        self.assertEqual(repo.refs.read_raw("HEAD"), "ref: refs/heads/main")
        self.assertIsNone(repo.head_commit())
        self.assertEqual(repo.config.get("remote.origin.url"), self.url)
        self.assertEqual(repo.worktree, os.path.join(self.tmp, "alice"))

    def test_clone_with_userinfo_saves_credentials_and_strips_url(self):
        url = f"http://alice:{self.tokens['alice']}@127.0.0.1:{self.httpd.server_address[1]}/proj"
        repo, _ = R.clone(url, None, self.tmp)  # dir name guessed: "proj"
        self.assertEqual(repo.worktree, os.path.join(self.tmp, "proj"))
        self.assertEqual(repo.config.get("remote.origin.url"), self.url.replace("://", "://alice@"))
        self.assertEqual(T.load_credentials(self.url.replace("://", "://alice@")), ("alice", self.tokens["alice"]))
        self.assertIsNone(T.load_credentials(self.url), "a userinfo URL does not set the host default")
        # the saved credentials are then used without env or userinfo
        self.commit(repo, {"x": b"1\n"}, "x")
        self.assertEqual(R.push(repo)[0], f"To {self.url.replace('://', '://alice@')}")

    def test_userinfo_clone_does_not_hijack_other_clones(self):
        # QA bug 1: alice is logged in; cloning once with bob's token in the URL must not make
        # alice's own clone (remote URL without a user) push as bob.
        R.login(self.url, "alice", self.tokens["alice"])
        mine, _ = R.clone(self.url, "mine", self.tmp)
        port = self.httpd.server_address[1]
        R.clone(f"http://bob:{self.tokens['bob']}@127.0.0.1:{port}/proj", "bobs", self.tmp)
        sha = self.commit(mine, {"eve.txt": b"x\n"}, "x")
        R.push(mine)
        self.assertEqual(self.server_ref(), sha)
        with open(os.path.join(self.root, server.AUDIT_LOG), "rb") as fh:
            last = fh.read().decode("utf-8").strip().splitlines()[-1]
        self.assertEqual(last.split()[1], "alice", last)
        self.assertEqual(T.load_credentials(self.url), ("alice", self.tokens["alice"]))

    def test_clone_nonempty_bytes_identical(self):
        files = {"crlf.txt": b"one\r\ntwo\r\n", "bin.dat": bytes(range(256)) * 4 + b"\x00\r\n",
                 "noeol.txt": b"no newline", "sub/deep/x.txt": b"x\n"}
        _, sha = self.alice_with_commit(files)
        bob, msgs = self.clone("bob")
        self.assertEqual(msgs, ["Cloning into 'bob'..."])
        self.assertEqual(bob.head_commit(), sha)
        self.assertEqual(bob.refs.read_raw("HEAD"), "ref: refs/heads/main")
        self.assertEqual(bob.refs.resolve("refs/remotes/origin/main"), sha)
        self.assertEqual(bob.config.get("branch.main.remote"), "origin")
        self.assertEqual(bob.config.get("branch.main.merge"), "refs/heads/main")
        for path, data in files.items():
            self.assertEqual(self.read(bob, path), data, path)
        self.assertEqual(index.read_index(bob).flat(), trees.commit_flat(bob.objects, sha))

    def test_clone_picks_server_head_branch(self):
        alice, _ = self.clone("alice")
        alice.refs.set_symbolic_head("refs/heads/dev")
        self.commit(alice, {"d": b"d\n"}, "dev work")
        self.push("alice", alice)
        bob, _ = self.clone("bob")
        self.assertEqual(bob.refs.read_raw("HEAD"), "ref: refs/heads/dev")
        self.assertEqual(self.read(bob, "d"), b"d\n")

    def test_clone_into_nonempty_dir_refused(self):
        os.makedirs(os.path.join(self.tmp, "busy"))
        with open(os.path.join(self.tmp, "busy", "f"), "wb") as fh:
            fh.write(b"x")
        with self.assertRaises(errors.MinigitError) as cm:
            self.clone("alice", "busy")
        self.assertIn("destination path 'busy' already exists and is not an empty directory", str(cm.exception))

    def test_clone_into_existing_empty_dir(self):
        os.makedirs(os.path.join(self.tmp, "empty"))
        repo, _ = self.clone("alice", "empty")
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "empty", ".minigit")))

    def test_failed_clone_removes_directory(self):
        self.alice_with_commit()
        with mock.patch.object(R, "_fetch_with", side_effect=errors.ObjectError("missing object x")):
            with self.assertRaises(errors.ObjectError):
                self.clone("bob")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "bob")))
        os.makedirs(os.path.join(self.tmp, "keep"))
        with mock.patch.object(R, "_fetch_with", side_effect=errors.ObjectError("missing object x")):
            with self.assertRaises(errors.ObjectError):
                self.clone("bob", "keep")
        self.assertEqual(os.listdir(os.path.join(self.tmp, "keep")), [])

    def test_clone_unknown_repo_and_bad_auth(self):
        with self.assertRaises(errors.RemoteError) as cm:
            self.clone("alice", "nope", url=self.base + "/nope")
        self.assertEqual(cm.exception.status, 404)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope")))
        with self.as_user("alice", token="wrong"):
            with self.assertRaises(errors.AuthError) as cm:
                R.clone(self.url, "bad", self.tmp)
        self.assertIn("authentication failed", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "bad")))
        with self.assertRaises(errors.AuthError):  # no credentials at all
            R.clone(self.url, "anon", self.tmp)

    def test_login_then_clone(self):
        R.login(self.url, "bob", self.tokens["bob"])
        repo, _ = R.clone(self.url, "viafile", self.tmp)
        self.assertIsNotNone(repo)

    def test_list_repos(self):
        with self.as_user("alice"):
            self.assertEqual(T.HttpRemote(self.url).list_repos(), ["proj"])


class PushFetchTests(LiveServerCase):
    def test_push_new_branch_with_upstream(self):
        alice, _ = self.clone("alice")
        sha = self.commit(alice, {"a": b"a\n"}, "a")
        lines = self.push("alice", alice, set_upstream=True)
        self.assertEqual(lines, [f"To {self.url}", " * [new branch]      main -> main",
                                 "branch 'main' set up to track 'origin/main'."])
        self.assertEqual(self.server_ref(), sha)
        self.assertEqual(alice.refs.resolve("refs/remotes/origin/main"), sha)
        self.assertEqual(alice.config.get("branch.main.merge"), "refs/heads/main")
        self.assertEqual(self.push("alice", alice), ["Everything up-to-date"])

    def test_push_fast_forward_line(self):
        alice, first = self.alice_with_commit()
        second = self.commit(alice, {"a.txt": b"changed\n"}, "second")
        lines = self.push("alice", alice)
        self.assertEqual(lines, [f"To {self.url}", f"   {first[:7]}..{second[:7]}  main -> main"])
        self.assertEqual(self.server_ref(), second)

    def test_only_missing_objects_are_sent(self):
        alice, _ = self.alice_with_commit({"a.txt": b"a\n", "b.txt": b"b\n", "dir/c.txt": b"c\n"})
        bob, _ = self.clone("bob")
        self.commit(alice, {"dir/c.txt": b"c2\n"}, "change c")
        sent = []
        orig_push = T.HttpRemote.push

        def spy_push(rem, updates, data, *, force=False):
            sent.append(data)
            return orig_push(rem, updates, data, force=force)

        with mock.patch.object(T.HttpRemote, "push", spy_push):
            self.push("alice", alice)
        # commit + root tree + dir tree + new blob
        self.assertEqual(pack.pack_count(sent[0]), 4)

        received = []
        orig_fetch = T.HttpRemote.fetch_pack

        def spy_fetch(rem, wants, haves):
            data = orig_fetch(rem, wants, haves)
            received.append((wants, haves, data))
            return data

        with mock.patch.object(T.HttpRemote, "fetch_pack", spy_fetch):
            changes = self.fetch("bob", bob)
            self.assertEqual(len(received), 1)
            self.assertEqual(pack.pack_count(received[0][2]), 4)
            self.assertIn(bob.head_commit(), received[0][1])
            self.assertEqual([c.kind for c in changes], ["updated"])
            # a second fetch has nothing to want: no fetch request at all
            changes = self.fetch("bob", bob)
            self.assertEqual(len(received), 1)
            self.assertEqual([c.kind for c in changes], ["up-to-date"])
            self.assertEqual(R.format_fetch(self.url, changes), "")

    def test_fetch_new_updated_deleted_branches(self):
        alice, first = self.alice_with_commit()
        bob, _ = self.clone("bob")
        self.push("alice", alice, None, "main:feature")
        second = self.commit(alice, {"a.txt": b"2\n"}, "two")
        self.push("alice", alice)
        changes = self.fetch("bob", bob)
        self.assertEqual(R.format_fetch(self.url, changes).split("\n"), [
            f"From {self.url}",
            " * [new branch]      feature -> origin/feature",
            f"   {first[:7]}..{second[:7]}  main -> origin/main",
        ])
        self.assertEqual(bob.refs.resolve("refs/remotes/origin/feature"), first)
        lines = self.push("alice", alice, None, "feature", delete=True)
        self.assertEqual(lines, [f"To {self.url}", " - [deleted]         feature"])
        self.assertIsNone(self.server_ref("refs/heads/feature"))
        self.assertIsNone(alice.refs.resolve("refs/remotes/origin/feature"))
        changes = self.fetch("bob", bob)
        self.assertEqual([(c.ref, c.kind) for c in changes],
                         [("refs/remotes/origin/main", "up-to-date"),
                          ("refs/remotes/origin/feature", "deleted")])
        self.assertIsNone(bob.refs.resolve("refs/remotes/origin/feature"))

    def test_delete_default_branch_rejected(self):
        alice, sha = self.alice_with_commit()
        with self.assertRaises(errors.PushRejectedError) as cm:
            self.push("alice", alice, None, "main", delete=True)
        self.assertEqual(cm.exception.results[0]["reason"], "delete-default-branch")
        self.assertIn("[rejected]", R.format_push_rejection(cm.exception))
        self.assertEqual(self.server_ref(), sha)

    def test_delete_missing_remote_branch(self):
        alice, _ = self.alice_with_commit()
        with self.assertRaises(errors.MinigitError):
            self.push("alice", alice, None, "ghost", delete=True)

    def test_non_ff_rejection_fetch_first_then_non_ff(self):
        alice, base = self.alice_with_commit()
        bob, _ = self.clone("bob")
        bob_sha = self.commit(bob, {"bob.txt": b"bob\n"}, "bob work")
        self.push("bob", bob)
        self.commit(alice, {"alice.txt": b"alice\n"}, "alice work")
        with self.assertRaises(errors.PushRejectedError) as cm:
            self.push("alice", alice)
        text = R.format_push_rejection(cm.exception, "main", "main")
        self.assertIn(" ! [rejected]        main -> main (fetch first)", text)
        self.assertIn("'minigit pull'", text)
        self.assertEqual(R.format_push_rejection(cm.exception), text)  # src/dst attached by push
        self.assertEqual(self.server_ref(), bob_sha)
        # after a fetch (but no merge) the local pre-check says non-fast-forward
        self.fetch("alice", alice)
        with self.assertRaises(errors.PushRejectedError) as cm:
            self.push("alice", alice)
        text = R.format_push_rejection(cm.exception, "main", "main")
        self.assertIn("(non-fast-forward)", text)
        self.assertIn("hint: Integrate the remote changes first ('minigit pull'), then push again.", text)
        self.assertEqual(self.server_ref(), bob_sha)
        self.assertEqual(alice.refs.resolve("refs/remotes/origin/main"), bob_sha)
        self.assertNotEqual(base, bob_sha)

    def test_server_side_stale_rejection(self):
        alice, _ = self.alice_with_commit()
        bob, _ = self.clone("bob")
        self.commit(alice, {"a.txt": b"alice\n"}, "alice 2")
        bob_sha = self.commit(bob, {"b.txt": b"bob\n"}, "bob 2")
        orig_info = T.HttpRemote.info_refs
        raced = []

        def racing_info_refs(rem):
            info = orig_info(rem)
            if not raced:  # bob sneaks in between alice's info/refs and her push
                raced.append(True)
                self.push("bob", bob)
            return info

        with mock.patch.object(T.HttpRemote, "info_refs", racing_info_refs):
            with self.assertRaises(errors.PushRejectedError) as cm:
                self.push("alice", alice)
        self.assertEqual(cm.exception.results[0]["reason"], "stale")
        text = R.format_push_rejection(cm.exception)
        self.assertIn("main -> main (fetch first)", text)
        self.assertIn("'minigit pull'", text)
        self.assertEqual(self.server_ref(), bob_sha)

    def test_force_disabled(self):
        alice, first = self.alice_with_commit()
        self.commit(alice, {"a.txt": b"2\n"}, "two")
        self.push("alice", alice)
        alice.refs.update_head(first)  # rewrite history: drop "two"
        self.commit(alice, {"a.txt": b"3\n"}, "three")
        with self.assertRaises(errors.PushRejectedError) as cm:
            self.push("alice", alice, force=True)
        self.assertEqual(cm.exception.results[0]["reason"], "force-disabled")
        text = R.format_push_rejection(cm.exception)
        self.assertIn("hint: this repository does not allow forced pushes", text)
        self.assertNotIn("minigit pull", text)
        self.assertNotIn("remote contains work", text)

    def test_too_long_branch_name_rejected_locally_and_by_the_server(self):
        # QA bug 7: the length check lives in refs.check_ref_name, shared by client and server
        alice, sha = self.alice_with_commit()
        long_name = "b" * 256
        with mock.patch.object(T.HttpRemote, "info_refs", side_effect=AssertionError("server contacted")):
            with self.assertRaises(errors.UsageError) as cm:
                self.push("alice", alice, "origin", f"main:{long_name}")
        self.assertEqual(cm.exception.message, f"invalid destination branch name '{long_name}': too long "
                                               "(a component is over 250 bytes)")
        with self.as_user("alice"):  # a client without the check: the server answers 400, not 500
            with self.assertRaises(errors.RemoteError) as cm:
                T.HttpRemote(self.url).push([{"ref": "refs/heads/" + long_name, "old": None, "new": sha}], b"")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("too long", str(cm.exception))
        self.assertIsNone(self.server_ref("refs/heads/" + long_name))

    @unittest.skipUnless(os.name == "nt", "Windows reserved names are checked on Windows")
    def test_reserved_windows_name_rejected_before_contacting_the_server(self):
        # QA bug 5: `push origin main:CON` fails locally with the reserved-name message
        alice, _ = self.alice_with_commit()
        for dst in ("CON", "aux.txt", "x/nul"):
            with self.subTest(dst=dst):
                with mock.patch.object(T.HttpRemote, "info_refs", side_effect=AssertionError("server contacted")):
                    with self.assertRaises(errors.UsageError) as cm:
                        self.push("alice", alice, "origin", f"main:{dst}")
                self.assertEqual(cm.exception.message, f"invalid destination branch name '{dst}': "
                                 f"'{dst.rsplit('/', 1)[-1]}' is a reserved device name on Windows")
        self.assertEqual(sorted(self.server_repo.refs.list("refs/heads/")), ["refs/heads/main"])

    def test_detached_head_needs_refspec(self):
        alice, sha = self.alice_with_commit()
        alice.refs.set_detached_head(sha)
        with self.assertRaises(errors.MinigitError):
            self.push("alice", alice)
        lines = self.push("alice", alice, "origin", "HEAD:topic")
        self.assertEqual(lines, [f"To {self.url}", " * [new branch]      HEAD -> topic"])
        self.assertEqual(self.server_ref("refs/heads/topic"), sha)

    def test_push_head_goes_to_the_current_branch(self):
        # 'push origin HEAD' is the current branch under its own name, as in git (it once made
        # a branch called "HEAD" on the server, which then came back as origin/HEAD)
        alice, sha = self.alice_with_commit()
        alice.refs.update("refs/heads/topic", sha, expected=None)
        alice.refs.set_symbolic_head("refs/heads/topic")
        lines = self.push("alice", alice, "origin", "HEAD", set_upstream=True)
        self.assertEqual(lines, [f"To {self.url}", " * [new branch]      topic -> topic",
                                 "branch 'topic' set up to track 'origin/topic'."])
        self.assertEqual(self.server_ref("refs/heads/topic"), sha)
        self.assertIsNone(self.server_ref("refs/heads/HEAD"))
        alice.refs.set_detached_head(sha)
        with self.assertRaises(errors.MinigitError) as cm:
            self.push("alice", alice, "origin", "HEAD")
        self.assertIn("HEAD:<branch>", cm.exception.message)

    def test_branch_named_head_is_refused(self):
        alice, sha = self.alice_with_commit()
        with self.assertRaises(errors.UsageError) as cm:
            self.push("alice", alice, "origin", "main:HEAD")
        self.assertIn("'HEAD' is not a valid branch name", cm.exception.message)
        # the server refuses it too, whatever the client sends
        rem = T.HttpRemote(self.url)
        with self.as_user("alice"):
            rem.credentials = T.load_credentials(self.url)
            with self.assertRaises(errors.RemoteError) as cm:
                rem.push([{"ref": "refs/heads/HEAD", "old": None, "new": sha}], pack.build_pack(alice.objects, []))
        self.assertEqual(cm.exception.status, 400)
        self.assertIsNone(self.server_ref("refs/heads/HEAD"))

    def test_up_to_date_push_with_u_prints_the_upstream_first(self):
        alice, _ = self.alice_with_commit()
        alice.config.unset("branch.main.remote")
        alice.config.unset("branch.main.merge")
        self.assertEqual(self.push("alice", alice, "origin", "main", set_upstream=True),
                         ["branch 'main' set up to track 'origin/main'.", "Everything up-to-date"])

    def test_push_unborn_branch_is_an_error(self):
        alice, _ = self.clone("alice")
        with self.assertRaises(errors.MinigitError) as cm:
            self.push("alice", alice)
        self.assertIn("does not match any commit", str(cm.exception))

    def test_read_only_user_permission_denied(self):
        self.alice_with_commit()
        reader, _ = self.clone("reader")
        self.commit(reader, {"r": b"r\n"}, "reader")
        with self.assertRaises(errors.PermissionDeniedError) as cm:
            self.push("reader", reader)
        self.assertTrue(str(cm.exception).startswith("permission denied"))

    def test_unknown_remote(self):
        alice, _ = self.clone("alice")
        with self.assertRaises(errors.MinigitError) as cm:
            self.fetch("alice", alice, "upstream")
        self.assertEqual(str(cm.exception), "no remote 'upstream' configured")


class ForcePushTests(LiveServerCase):
    allow_force = True

    def test_forced_update_lines(self):
        alice, first = self.alice_with_commit()
        bob, _ = self.clone("bob")
        two = self.commit(alice, {"a.txt": b"2\n"}, "two")
        self.push("alice", alice)
        self.fetch("bob", bob)
        alice.refs.update_head(first)
        three = self.commit(alice, {"a.txt": b"3\n"}, "three")
        lines = self.push("alice", alice, force=True)
        self.assertEqual(lines[1], f" + {two[:7]}...{three[:7]} main -> main (forced update)")
        self.assertEqual(self.server_ref(), three)
        changes = self.fetch("bob", bob)
        self.assertEqual(R.format_fetch(self.url, changes).split("\n")[1],
                         f" + {two[:7]}...{three[:7]} main -> origin/main (forced update)")


class PushUrlDisplayTests(LiveServerCase):
    def test_server_rejection_shows_the_configured_url(self):
        # a local pre-check and a server 409 print the same "To <url>" (the user name stays, never a token)
        alice, first = self.alice_with_commit()
        user_url = self.url.replace("://", "://alice@")
        alice.config.set("remote.origin.url", user_url)
        self.commit(alice, {"a.txt": b"2\n"}, "two")
        self.push("alice", alice)
        alice.refs.update_head(first)
        self.commit(alice, {"a.txt": b"3\n"}, "three")
        with self.assertRaises(errors.PushRejectedError) as cm:
            self.push("alice", alice)  # local pre-check: non-fast-forward
        self.assertEqual(cm.exception.url, user_url)
        alice.config.set("remote.origin.url", self.url.replace("://", f"://alice:{self.tokens['alice']}@"))
        with self.assertRaises(errors.PushRejectedError) as cm:
            self.push("alice", alice, force=True)  # server 409 (this repo has allow_force off)
        text = R.format_push_rejection(cm.exception)
        self.assertIn(f"To {self.url.replace('://', '://alice:***@')}\n", text)
        self.assertNotIn(self.tokens["alice"], text)
        self.assertIn("(forced update disabled)", text)

    def test_fetch_header_never_shows_a_token(self):
        alice, _ = self.alice_with_commit()
        bob, _ = self.clone("bob")
        self.commit(alice, {"a.txt": b"2\n"}, "two")
        self.push("alice", alice)
        changes = self.fetch("bob", bob)
        text = R.format_fetch("http://bob:SECRET@h:1/proj", changes)
        self.assertTrue(text.startswith("From http://bob:***@h:1/proj\n"), text)


class TagTransportTests(LiveServerCase):
    def tag(self, repo, name, target, message=None):
        from minigit import tags
        tags.tag_create(repo, name, target, messages=[message] if message else None)
        return repo.refs.resolve("refs/tags/" + name)

    def test_push_tag_lines_and_clone_brings_tags(self):
        alice, first = self.alice_with_commit()
        second = self.commit(alice, {"a.txt": b"2\n"}, "second")
        light = self.tag(alice, "v0", first)
        ann = self.tag(alice, "v1", second, "release one")
        self.assertNotEqual(ann, second)
        # a tag is pushed by its short name; the branch is still unpushed, so the pack has the commit too
        lines = self.push("alice", alice, None, "v1")
        self.assertEqual(lines, [f"To {self.url}", " * [new tag]         v1 -> v1"])
        self.assertEqual(self.server_ref("refs/tags/v1"), ann)
        self.assertEqual(self.server_repo.objects.read(ann)[0], "tag")
        self.assertEqual(pack.fsck(self.server_repo), [])
        self.assertIsNone(alice.refs.resolve("refs/remotes/origin/v1"), "tags have no tracking namespace")
        self.assertEqual(self.push("alice", alice, None, "refs/tags/v1"), ["Everything up-to-date"])
        lines = self.push("alice", alice, None, "refs/tags/v0:v0")
        self.assertEqual(lines[1], " * [new tag]         v0 -> v0")
        self.push("alice", alice)
        # a clone gets every tag whose commit it fetched
        bob, _ = self.clone("bob")
        self.assertEqual(bob.refs.resolve("refs/tags/v0"), light)
        self.assertEqual(bob.refs.resolve("refs/tags/v1"), ann)
        self.assertEqual(bob.objects.read(ann)[0], "tag")
        from minigit.revparse import resolve_rev
        self.assertEqual(resolve_rev(bob, "v1"), second)

    def test_fetch_auto_follows_tags_and_tags_option(self):
        alice, first = self.alice_with_commit()
        bob, _ = self.clone("bob")
        second = self.commit(alice, {"a.txt": b"2\n"}, "second")
        ann = self.tag(alice, "v1", second, "release one")
        # an orphan commit (not on any branch) with a lightweight tag
        alice.refs.set_detached_head(first)
        orphan = self.commit(alice, {"o.txt": b"o\n"}, "orphan")
        alice.refs.set_symbolic_head("refs/heads/main")
        orphan_tag = self.tag(alice, "orphan", orphan)
        self.push("alice", alice, None, "v1")
        self.push("alice", alice, None, "orphan")
        self.push("alice", alice)
        changes = self.fetch("bob", bob)
        self.assertEqual(R.format_fetch(self.url, changes).split("\n"), [
            f"From {self.url}",
            f"   {first[:7]}..{second[:7]}  main -> origin/main",
            " * [new tag]         v1 -> v1",
        ])
        self.assertEqual(bob.refs.resolve("refs/tags/v1"), ann)
        self.assertIsNone(bob.refs.resolve("refs/tags/orphan"), "its commit is on no fetched branch")
        self.assertFalse(bob.objects.has(orphan))
        # fetch --tags brings every tag, with the objects they need
        with self.as_user("bob"):
            changes = R.fetch(bob, "origin", all_tags=True)
        self.assertEqual(R.format_fetch(self.url, changes).split("\n"),
                         [f"From {self.url}", " * [new tag]         orphan -> orphan"])
        self.assertEqual(bob.refs.resolve("refs/tags/orphan"), orphan_tag)
        self.assertTrue(bob.objects.has(orphan))
        self.assertEqual(pack.fsck(bob), [])
        # nothing new: no lines, and no request for objects
        with mock.patch.object(T.HttpRemote, "fetch_pack", side_effect=AssertionError("no fetch expected")):
            changes = self.fetch("bob", bob)
        self.assertEqual(R.format_fetch(self.url, changes), "")
        # a local tag that differs is never clobbered by fetch
        bob.refs.update("refs/tags/v1", first)
        changes = self.fetch("bob", bob)
        self.assertEqual(R.format_fetch(self.url, changes).split("\n"),
                         [f"From {self.url}", " ! [rejected]        v1 -> v1 (would clobber existing tag)"])
        self.assertEqual(bob.refs.resolve("refs/tags/v1"), first)

    def test_tag_exists_rejection_delete_and_push_tags(self):
        alice, first = self.alice_with_commit()
        second = self.commit(alice, {"a.txt": b"2\n"}, "second")
        self.tag(alice, "v1", first)
        self.push("alice", alice, None, "v1")
        # moving the tag locally and pushing again: rejected before upload, git's wording
        alice.refs.update("refs/tags/v1", second)
        with self.assertRaises(errors.PushRejectedError) as cm:
            self.push("alice", alice, None, "v1")
        self.assertEqual(cm.exception.results[0]["reason"], "tag-exists")
        text = R.format_push_rejection(cm.exception)
        self.assertIn(" ! [rejected]        v1 -> v1 (already exists)", text)
        self.assertIn("hint: Updates were rejected because the tag already exists in the remote.", text)
        self.assertNotIn("'minigit pull'", text)
        self.assertEqual(self.server_ref("refs/tags/v1"), first)
        # --force on a repository that forbids it: the server says so
        with self.assertRaises(errors.PushRejectedError) as cm:
            self.push("alice", alice, None, "v1", force=True)
        self.assertEqual(cm.exception.results[0]["reason"], "force-disabled")
        # push --tags: new tags go up, the differing one is reported, exit is a rejection
        self.tag(alice, "v2", second)
        self.tag(alice, "v3", second, "three")
        with self.assertRaises(errors.PushRejectedError) as cm:
            with self.as_user("alice"):
                R.push_tags(alice, "origin")
        self.assertEqual(self.server_ref("refs/tags/v2"), second)
        self.assertEqual(self.server_ref("refs/tags/v3"), alice.refs.resolve("refs/tags/v3"))
        text = R.format_push_rejection(cm.exception)
        self.assertIn(" * [new tag]         v2 -> v2", text)
        self.assertIn(" * [new tag]         v3 -> v3", text)
        self.assertIn(" ! [rejected]        v1 -> v1 (already exists)", text)
        # delete on the server, then push --tags is clean
        lines = self.push("alice", alice, None, "v1", delete=True)
        self.assertEqual(lines, [f"To {self.url}", " - [deleted]         v1"])
        self.assertIsNone(self.server_ref("refs/tags/v1"))
        with self.as_user("alice"):
            lines = R.push_tags(alice, "origin")
        self.assertEqual(lines, [f"To {self.url}", " * [new tag]         v1 -> v1"])
        self.assertEqual(self.server_ref("refs/tags/v1"), second)
        with self.as_user("alice"):
            self.assertEqual(R.push_tags(alice, "origin"), ["Everything up-to-date"])
        # --delete of a name that is both a branch and a tag on the server is ambiguous
        self.push("alice", alice, None, "main:v1")
        with self.assertRaises(errors.MinigitError) as cm:
            self.push("alice", alice, None, "v1", delete=True)
        self.assertIn("matches more than one", cm.exception.message)
        self.push("alice", alice, None, "refs/heads/v1", delete=True)
        self.assertIsNone(self.server_ref("refs/heads/v1"))
        self.assertEqual(self.server_ref("refs/tags/v1"), second)
        self.assertEqual(pack.fsck(self.server_repo), [])


class TagForcePushTests(LiveServerCase):
    allow_force = True

    def test_force_replaces_a_tag(self):
        alice, first = self.alice_with_commit()
        second = self.commit(alice, {"a.txt": b"2\n"}, "second")
        alice.refs.update("refs/tags/v1", first)
        self.push("alice", alice, None, "v1")
        alice.refs.update("refs/tags/v1", second)
        lines = self.push("alice", alice, None, "v1", force=True)
        self.assertEqual(lines[1], f" + {first[:7]}...{second[:7]} v1 -> v1 (forced update)")
        self.assertEqual(self.server_ref("refs/tags/v1"), second)


class PullTests(LiveServerCase):
    def test_two_clones_of_an_empty_repo_need_allow_unrelated(self):
        # QA bug 12: both clone the fresh empty repo and each make a root commit
        alice, _ = self.clone("alice")
        bob, _ = self.clone("bob")
        alice_root = self.commit(alice, {"a.txt": b"alice\n"}, "alice root")
        self.push("alice", alice)
        bob_root = self.commit(bob, {"b.txt": b"bob\n"}, "bob root")
        with self.assertRaises(errors.MinigitError) as cm:
            self.pull("bob", bob)
        self.assertEqual(cm.exception.message, "refusing to merge unrelated histories")
        self.assertIn("--allow-unrelated-histories", cm.exception.hint)
        self.assertEqual(bob.head_commit(), bob_root)
        with self.as_user("bob"):
            _, outcome = R.pull(bob, allow_unrelated=True)
        self.assertEqual(outcome.kind, "merged")
        self.assertEqual((self.read(bob, "a.txt"), self.read(bob, "b.txt")), (b"alice\n", b"bob\n"))
        self.push("bob", bob)
        self.assertEqual(self.server_ref(), bob.head_commit())
        self.assertNotEqual(bob.head_commit(), alice_root)

    def test_pull_fast_forward(self):
        alice, _ = self.alice_with_commit()
        bob, _ = self.clone("bob")
        new = self.commit(alice, {"a.txt": b"updated\n", "new.txt": b"n\n"}, "update")
        self.push("alice", alice)
        changes, outcome = self.pull("bob", bob)
        self.assertEqual([c.kind for c in changes], ["updated"])
        self.assertEqual(outcome.kind, "fast-forward")
        self.assertEqual(bob.head_commit(), new)
        self.assertEqual(self.read(bob, "a.txt"), b"updated\n")
        self.assertEqual(self.read(bob, "new.txt"), b"n\n")
        _, outcome = self.pull("bob", bob)
        self.assertEqual(outcome.kind, "up-to-date")

    def test_pull_merge_then_push_converges(self):
        alice, _ = self.alice_with_commit()
        bob, _ = self.clone("bob")
        self.commit(bob, {"bob.txt": b"bob\n"}, "bob")
        self.push("bob", bob)
        self.commit(alice, {"alice.txt": b"alice\n"}, "alice")
        with self.assertRaises(errors.PushRejectedError):
            self.push("alice", alice)
        _, outcome = self.pull("alice", alice)
        self.assertEqual(outcome.kind, "merged")
        merge_commit = alice.read_commit(alice.head_commit())
        self.assertEqual(len(merge_commit.parents), 2)
        self.assertEqual(merge_commit.message, f"Merge branch 'main' of {self.url}\n")
        self.assertEqual(self.read(alice, "bob.txt"), b"bob\n")
        self.push("alice", alice)
        _, outcome = self.pull("bob", bob)
        self.assertEqual(outcome.kind, "fast-forward")
        self.assertEqual(bob.head_commit(), alice.head_commit())
        self.assertEqual(self.server_ref(), alice.head_commit())

    def test_pull_merge_message_has_no_user_name(self):
        # QA local bug 4: the user name of the clone URL must not end up in history
        alice, _ = self.alice_with_commit()
        with_user = self.url.replace("//", "//alice@", 1)
        alice2, _ = self.clone("alice", "alice2", url=with_user)
        self.assertIn("alice@", R.remote_url(alice2, "origin"))
        bob, _ = self.clone("bob")
        self.commit(bob, {"bob.txt": b"bob\n"}, "bob")
        self.push("bob", bob)
        self.commit(alice2, {"x.txt": b"x\n"}, "x")
        _, outcome = self.pull("alice", alice2)
        self.assertEqual(outcome.kind, "merged")
        message = alice2.read_commit(alice2.head_commit()).message
        self.assertEqual(message, f"Merge branch 'main' of {self.url}\n")
        self.assertNotIn("alice@", message)

    def test_pull_conflict_writes_markers(self):
        alice, _ = self.alice_with_commit({"f.txt": b"line\n"})
        bob, _ = self.clone("bob")
        self.commit(bob, {"f.txt": b"bob\n"}, "bob")
        self.push("bob", bob)
        self.commit(alice, {"f.txt": b"alice\n"}, "alice")
        _, outcome = self.pull("alice", alice)
        self.assertEqual(outcome.kind, "conflict")
        self.assertEqual(outcome.conflicts, ["f.txt"])
        content = self.read(alice, "f.txt")
        self.assertIn(b"<<<<<<< HEAD\nalice\n=======\nbob\n>>>>>>> origin/main", content)
        self.assertIsNotNone(alice.merge_head())

    def test_pull_without_upstream(self):
        alice, sha = self.alice_with_commit()
        alice.refs.update("refs/heads/feature", sha, expected=None)
        alice.refs.set_symbolic_head("refs/heads/feature")
        with self.assertRaises(errors.MinigitError) as cm:
            self.pull("alice", alice)
        self.assertEqual(str(cm.exception), "no upstream 'origin/feature' (push with -u first)")
        self.assertIn("minigit push -u origin feature", cm.exception.hint)

    def test_refused_merge_still_reports_the_fetch(self):
        # the fetch moved origin/main before the merge refused: the CLI prints it, as git does
        alice, _ = self.alice_with_commit()
        bob, _ = self.clone("bob")
        new = self.commit(alice, {"a.txt": b"updated\n"}, "update")
        self.push("alice", alice)
        with open(bob.abspath("a.txt"), "wb") as fh:
            fh.write(b"dirty\n")
        with self.assertRaises(errors.MinigitError) as cm:
            self.pull("bob", bob)
        changes = cm.exception.fetch_changes
        self.assertEqual([(c.ref, c.kind, c.new) for c in changes], [("refs/remotes/origin/main", "updated", new)])
        self.assertEqual(bob.refs.resolve("refs/remotes/origin/main"), new)

    def test_pull_into_empty_clone(self):
        bob, _ = self.clone("bob")
        _, sha = self.alice_with_commit()
        _, outcome = self.pull("bob", bob)
        self.assertEqual(outcome.kind, "fast-forward")
        self.assertEqual(bob.head_commit(), sha)
        self.assertEqual(self.read(bob, "a.txt"), b"alpha\n")


class PullRebaseTests(LiveServerCase):
    def pull_rebase(self, who, repo, *args):
        with self.as_user(who):
            return R.pull(repo, *args, rebase=True)

    def test_rebase_replays_own_commits_on_top_of_the_fetched_branch(self):
        alice, base = self.alice_with_commit()
        bob, _ = self.clone("bob")
        theirs = self.commit(alice, {"alice.txt": b"alice\n"}, "alice work")
        self.push("alice", alice)
        mine = self.commit(bob, {"bob.txt": b"bob\n"}, "bob work")
        changes, outcome = self.pull_rebase("bob", bob)
        self.assertEqual([c.kind for c in changes], ["updated"])
        self.assertEqual(outcome.kind, "rebased")
        self.assertEqual(outcome.old_head, mine)
        self.assertEqual(outcome.messages, ["Successfully rebased and updated refs/heads/main."])
        head = bob.head_commit()
        self.assertEqual(outcome.commit, head)
        self.assertNotEqual(head, mine)
        replayed = bob.read_commit(head)
        self.assertEqual(replayed.parents, (theirs,))           # linear: bob work on top of alice work
        self.assertEqual(replayed.message, "bob work\n")
        self.assertEqual(bob.read_commit(theirs).parents, (base,))
        self.assertEqual((self.read(bob, "alice.txt"), self.read(bob, "bob.txt")), (b"alice\n", b"bob\n"))
        self.assertEqual(bob.refs.current_branch(), "main")
        self.assertIsNone(bob.merge_head())
        self.assertFalse(os.path.isdir(bob.gpath("rebase")))
        self.push("bob", bob)                                   # a fast-forward for the server
        self.assertEqual(self.server_ref(), head)
        # again: nothing to do
        _, outcome = self.pull_rebase("bob", bob)
        self.assertEqual((outcome.kind, outcome.messages), ("up-to-date", ["Already up to date."]))
        self.assertEqual(bob.head_commit(), head)

    def test_rebase_fast_forwards_without_own_commits(self):
        alice, _ = self.alice_with_commit()
        bob, _ = self.clone("bob")
        new = self.commit(alice, {"a.txt": b"updated\n"}, "update")
        self.push("alice", alice)
        _, outcome = self.pull_rebase("bob", bob)
        self.assertEqual(outcome.kind, "fast-forward")
        self.assertEqual(outcome.messages, ["Fast-forwarded main to origin/main."])
        self.assertEqual(bob.head_commit(), new)
        self.assertEqual(self.read(bob, "a.txt"), b"updated\n")
        self.assertEqual(bob.refs.current_branch(), "main")

    def test_rebase_already_up_to_date_when_ahead(self):
        alice, _ = self.alice_with_commit()
        mine = self.commit(alice, {"x.txt": b"x\n"}, "local only")
        _, outcome = self.pull_rebase("alice", alice)
        self.assertEqual((outcome.kind, outcome.messages), ("up-to-date", ["Already up to date."]))
        self.assertEqual(alice.head_commit(), mine)

    def test_rebase_conflict_stops_and_abort_or_continue_finish(self):
        alice, _ = self.alice_with_commit({"f.txt": b"line\n"})
        bob, _ = self.clone("bob")
        self.commit(bob, {"f.txt": b"bob\n"}, "bob")
        self.push("bob", bob)
        mine = self.commit(alice, {"f.txt": b"alice\n"}, "alice")
        _, outcome = self.pull_rebase("alice", alice)
        self.assertEqual(outcome.kind, "conflict")
        self.assertEqual(outcome.conflicts, ["f.txt"])
        self.assertEqual(outcome.messages[:2], ["Auto-merging f.txt", "CONFLICT (content): Merge conflict in f.txt"])
        self.assertEqual(outcome.messages[2], f"Could not apply {mine[:7]}... alice")
        self.assertIn('then run "minigit rebase --continue".', outcome.messages[4])
        self.assertIn(b"<<<<<<< HEAD\nbob\n=======\nalice\n>>>>>>> ", self.read(alice, "f.txt"))
        self.assertIsNone(alice.merge_head())
        self.assertEqual(alice.refs.read_special("REBASE_HEAD"), mine)
        self.assertEqual(alice.refs.resolve("refs/heads/main"), mine)   # the branch moves only at the end
        self.assertEqual(alice.operation_in_progress()[0], "rebase")
        from minigit import rebase as rebase_mod
        # the usual rebase state: --abort puts everything back
        rebase_mod.abort(alice)
        self.assertEqual(alice.head_commit(), mine)
        self.assertEqual(alice.refs.current_branch(), "main")
        self.assertEqual(self.read(alice, "f.txt"), b"alice\n")
        # ... and --continue finishes a resolved one
        _, outcome = self.pull_rebase("alice", alice)
        self.assertEqual(outcome.kind, "conflict")
        with open(alice.abspath("f.txt"), "wb") as fh:
            fh.write(b"both\n")
        with index.locked_index(alice) as idx:
            data = b"both\n"
            idx.put(index.IndexEntry(path="f.txt", mode="100644", sha=alice.objects.write("blob", data),
                                     size=len(data), mtime_ns=0))
        res = rebase_mod.continue_(alice)
        self.assertFalse(res.stopped)
        self.assertEqual(res.lines[-1], "Successfully rebased and updated refs/heads/main.")
        head = alice.head_commit()
        self.assertEqual(alice.read_commit(head).parents, (self.server_ref(),))
        self.assertEqual(alice.read_commit(head).message, "alice\n")
        self.push("alice", alice)
        self.assertEqual(self.server_ref(), head)

    def test_rebase_refuses_dirty_tree_unborn_head_and_open_rebase_before_fetching(self):
        alice, _ = self.alice_with_commit()
        bob, _ = self.clone("bob")
        self.commit(alice, {"a.txt": b"updated\n"}, "update")
        self.push("alice", alice)
        old_tracking = bob.refs.resolve("refs/remotes/origin/main")
        with open(bob.abspath("a.txt"), "ab") as fh:
            fh.write(b"local edit\n")
        with self.assertRaises(errors.MinigitError) as cm:
            self.pull_rebase("bob", bob)
        self.assertEqual(cm.exception.message, "cannot rebase: you have unstaged changes")
        self.assertEqual(bob.refs.resolve("refs/remotes/origin/main"), old_tracking)   # nothing fetched
        with index.locked_index(bob) as idx:
            data = b"alpha\nlocal edit\n"
            idx.put(index.IndexEntry(path="a.txt", mode="100644", sha=bob.objects.write("blob", data),
                                     size=len(data), mtime_ns=0))
        with self.assertRaises(errors.MinigitError) as cm:
            self.pull_rebase("bob", bob)
        self.assertEqual(cm.exception.message, "cannot rebase: your index contains uncommitted changes")
        # an unborn HEAD: merge-pull fast-forwards, rebase-pull refuses with rebase's message
        carol, _ = self.clone("bob", "carol")
        carol.refs.update("refs/heads/main", None, expected=carol.head_commit())
        with self.assertRaises(errors.MinigitError) as cm:
            self.pull_rebase("bob", carol)
        self.assertEqual(cm.exception.message,
                         "cannot rebase: your current branch 'main' does not have any commits yet")
        # a stopped rebase must be finished first
        dave, _ = self.clone("bob", "dave")
        self.commit(dave, {"a.txt": b"dave\n"}, "dave")
        self.commit(alice, {"a.txt": b"alice again\n"}, "alice again")
        self.push("alice", alice)
        _, outcome = self.pull_rebase("bob", dave)
        self.assertEqual(outcome.kind, "conflict")
        with self.assertRaises(errors.MinigitError) as cm:
            self.pull_rebase("bob", dave)
        self.assertEqual(cm.exception.message, "rebase in progress; finish it first")

    def test_no_rebase_is_the_default_merge(self):
        alice, _ = self.alice_with_commit()
        bob, _ = self.clone("bob")
        self.commit(alice, {"alice.txt": b"alice\n"}, "alice")
        self.push("alice", alice)
        self.commit(bob, {"bob.txt": b"bob\n"}, "bob")
        with self.as_user("bob"):
            _, outcome = R.pull(bob, rebase=False)
        self.assertEqual(outcome.kind, "merged")
        self.assertEqual(len(bob.read_commit(bob.head_commit()).parents), 2)


class RemoteConfigTests(_EnvCase):
    def test_add_list_remove(self):
        repo, _ = Repository.init(os.path.join(self.tmp, "r"))
        R.remote_add(repo, "origin", "http://alice:TOK@127.0.0.1:1/proj/")
        # the stored URL keeps the user name (never the token) so a clone remembers who it acts as
        self.assertEqual(R.remote_list(repo), {"origin": "http://alice@127.0.0.1:1/proj"})
        self.assertEqual(R.remote_url(repo, "origin"), "http://alice@127.0.0.1:1/proj")
        self.assertEqual(T.load_credentials(R.remote_url(repo, "origin")), ("alice", "TOK"))
        self.assertIsNone(T.load_credentials("http://127.0.0.1:1/proj"), "only login sets the host default")
        with self.assertRaises(errors.MinigitError):
            R.remote_add(repo, "origin", "http://127.0.0.1:1/other")
        with self.assertRaises(errors.MinigitError):
            R.remote_add(repo, "bad name", "http://127.0.0.1:1/other")
        with self.assertRaises(errors.MinigitError):
            R.remote_add(repo, "up", "ftp://127.0.0.1/other")
        sha = repo.objects.write("blob", b"x")  # any object; tracking refs only store a sha
        repo.refs.update("refs/remotes/origin/main", sha)
        repo.config.set("branch.main.remote", "origin")
        repo.config.set("branch.main.merge", "refs/heads/main")
        R.remote_remove(repo, "origin")
        self.assertEqual(R.remote_list(repo), {})
        self.assertEqual(repo.refs.list("refs/remotes/"), {})
        self.assertIsNone(repo.config.get("branch.main.remote"))
        with self.assertRaises(errors.MinigitError) as cm:
            R.remote_remove(repo, "origin")
        self.assertEqual(str(cm.exception), "no remote 'origin' configured")

    def test_listing_masks_a_hand_written_token(self):
        repo, _ = Repository.init(os.path.join(self.tmp, "r"))
        repo.config.set("remote.origin.url", "http://alice:SECRET@127.0.0.1:1/proj")
        self.assertEqual(R.remote_list(repo), {"origin": "http://alice:***@127.0.0.1:1/proj"})
        self.assertEqual(R.remote_url(repo, "origin"), "http://alice:SECRET@127.0.0.1:1/proj")


if __name__ == "__main__":
    unittest.main()

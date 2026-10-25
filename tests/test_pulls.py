"""tests.test_pulls -- owner: M5 server.

In-process tests for minigit.pulls (pull requests lite): the edge cases the end-to-end suite
(tests/test_pull_requests_lite.py) does not reach -- unrelated histories, a base ref moved out of band
during the merge, a file/directory clash, lock timeouts, a corrupt store, body limits.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from unittest import mock

from minigit import auth, pulls
from minigit.errors import MinigitError
from minigit.refs import RefStore
from tests.test_server import ServerTestCase, make_commit


class PullsTestCase(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.server_repo()
        self.store = self.repo.objects

    def mk(self, files: dict[str, bytes], parents: list[str], msg: str, ts: int = 1_700_000_000) -> str:
        return make_commit(self.store, files, parents, msg, ts)

    def branch(self, name: str, sha: str) -> None:
        self.repo.refs.update("refs/heads/" + name, sha)

    def tip(self, name: str) -> str | None:
        return self.repo.refs.resolve("refs/heads/" + name)

    def setup_pr(self, base_files=None, head_files=None) -> tuple[str, str]:
        root = self.mk(base_files or {"a.txt": b"a\n"}, [], "root")
        self.branch("main", root)
        head = self.mk(head_files or {"a.txt": b"a\n", "f.txt": b"f\n"}, [root], "feature")
        self.branch("feature", head)
        pulls.open_pull(self.repo, "alice", "feature", "main", "T")
        return root, head

    def assert_pr_error(self, status: int, code: str, fn, *args, **kw) -> pulls.PullRequestError:
        with self.assertRaises(pulls.PullRequestError) as cm:
            fn(*args, **kw)
        self.assertEqual((cm.exception.status, cm.exception.code), (status, code), cm.exception.message)
        return cm.exception


class TestPullsLibrary(PullsTestCase):
    def test_missing_store_is_empty_and_numbers_are_never_reused(self):
        self.assertEqual(pulls.load_pulls(self.repo), {"version": 1, "next": 1, "pulls": {}})
        self.setup_pr()
        pulls.close_pull(self.repo, "alice", 1)
        view = pulls.open_pull(self.repo, "alice", "refs/heads/feature", "main", "  again  ")
        self.assertEqual((view["number"], view["title"], view["head"]), (2, "again", "feature"))
        self.assertEqual(pulls.load_pulls(self.repo)["next"], 3)
        self.assertEqual([p["number"] for p in pulls.list_pulls(self.repo, "all")], [1, 2])

    def test_unrelated_histories(self):
        root = self.mk({"a.txt": b"a\n"}, [], "root")
        self.branch("main", root)
        other = self.mk({"b.txt": b"b\n"}, [], "other root")
        self.branch("other", other)
        view = pulls.open_pull(self.repo, "alice", "other", "main", "Unrelated")
        self.assertEqual(view["mergeable"], "unrelated")
        self.assertIsNone(view["merge_base"])
        self.assert_pr_error(409, "unrelated_histories", pulls.merge_pull, self.repo, "bob", 1)
        self.assert_pr_error(409, "unrelated_histories", pulls.pull_diff, self.repo, 1)
        self.assertEqual(self.tip("main"), root)
        self.assertEqual(pulls.get_pull(self.repo, 1)["state"], "open")

    def test_file_directory_clash_is_a_conflict(self):
        root = self.mk({"a.txt": b"a\n"}, [], "root")
        self.branch("main", root)
        main2 = self.mk({"a.txt": b"a\n", "d": b"file\n"}, [root], "d as file")
        self.branch("main", main2)
        store = self.store
        from minigit.objects import TreeEntry, serialize_tree
        sub = store.write("tree", serialize_tree([TreeEntry("100644", "x", store.write("blob", b"x\n"))]))
        top = store.write("tree", serialize_tree([TreeEntry("100644", "a.txt", store.write("blob", b"a\n")),
                                                  TreeEntry("40000", "d", sub)]))
        from minigit.objects import Commit, Signature, serialize_commit
        sig = Signature("T", "t@example.com", 1_700_000_000, 0)
        head = store.write("commit", serialize_commit(Commit(top, (root,), sig, sig, "d as dir\n")))
        self.branch("feature", head)
        view = pulls.open_pull(self.repo, "alice", "feature", "main", "Clash")
        self.assertEqual(view["mergeable"], "conflict")
        self.assertTrue(view["conflicts"])
        err = self.assert_pr_error(409, "merge_conflict", pulls.merge_pull, self.repo, "bob", 1)
        self.assertEqual((err.extra["head"], err.extra["base"]), ("feature", "main"))
        self.assertTrue(any("file/directory" in m for m in err.extra["messages"]), err.extra)
        self.assertEqual(self.tip("main"), main2)

    def test_base_moved_out_of_band(self):
        root, head = self.setup_pr()
        sneaky = self.mk({"a.txt": b"a\n", "s.txt": b"s\n"}, [root], "out of band")
        real_update = RefStore.update

        def update(refs, name, new, expected=None, timeout=0.0):
            real_update(refs, name, sneaky)  # someone rewrites the ref between the trial merge and the CAS
            return real_update(refs, name, new, expected=expected, timeout=timeout)

        with mock.patch.object(RefStore, "update", update):
            self.assert_pr_error(409, "base_moved", pulls.merge_pull, self.repo, "bob", 1)
        self.assertEqual(self.tip("main"), sneaky)
        self.assertEqual(pulls.get_pull(self.repo, 1)["state"], "open")
        result = pulls.merge_pull(self.repo, "bob", 1)
        self.assertEqual(result["old"], sneaky)
        self.assertEqual(self.repo.read_commit(result["new"]).parents, (sneaky, head))

    def test_up_to_date_after_hand_merge(self):
        root, head = self.setup_pr()
        self.branch("main", head)
        result = pulls.merge_pull(self.repo, "bob", 1)
        self.assertEqual((result["kind"], result["merge_commit"], result["old"], result["new"]),
                         ("up-to-date", head, head, head))
        self.assertEqual(result["pull"]["state"], "merged")

    def test_deleted_branch_lists_no_commits(self):
        # QA bug 8: with the base gone, 'pr show' listed head's whole history as "0 ahead" PR commits.
        root, head = self.setup_pr()
        self.assertEqual([c["sha"] for c in pulls.pull_view(self.repo, pulls.get_pull(self.repo, 1))["commits"]],
                         [head])
        for gone, other in (("main", "feature"), ("feature", "main")):
            with self.subTest(gone=gone):
                sha = self.tip(gone)
                self.repo.refs.update("refs/heads/" + gone, None)
                view = pulls.pull_view(self.repo, pulls.get_pull(self.repo, 1))
                self.assertEqual((view["commits"], view["ahead"], view["behind"]), ([], 0, 0))
                self.assertEqual((view["mergeable"], view["missing_branch"]), ("unknown", gone))
                self.assertIn(f"branch '{gone}' of pull request #1 no longer exists", view["messages"])
                self.branch(gone, sha)
        self.repo.refs.update("refs/heads/main", None)
        view = pulls.close_pull(self.repo, "alice", 1)["pull"]  # the snapshot keeps base_sha = null
        self.assertEqual((view["state"], view["base_sha"], view["commits"]), ("closed", None, []))
        self.assertEqual(view["missing_branch"], "main")
        stored = pulls.get_pull(self.repo, 1)
        self.assertNotIn("commits", stored)
        self.assertIsNone(pulls.pull_view(self.repo, stored)["mergeable"])
        self.branch("main", root)
        self.assertEqual(pulls.pull_view(self.repo, pulls.get_pull(self.repo, 1))["commits"], [])  # snapshot

    def test_merge_message_and_identity(self):
        root, head = self.setup_pr()
        self.assert_pr_error(400, "bad_request", pulls.merge_pull, self.repo, "bob", 1, " \n ")
        result = pulls.merge_pull(self.repo, "bob", 1, "Custom\n\n\n")
        c = self.repo.read_commit(result["merge_commit"])
        self.assertEqual(c.message, "Custom\n")
        self.assertEqual((c.author.name, c.author.email, c.committer.email), ("bob", "bob@minigit", "bob@minigit"))
        self.assertEqual(c.parents, (root, head))

    def test_repo_lock_timeout_is_busy(self):
        self.setup_pr()
        lock = threading.Lock()
        lock.acquire()
        try:
            self.assert_pr_error(503, "busy", pulls.merge_pull, self.repo, "bob", 1, lock=lock, lock_timeout=0.05)
        finally:
            lock.release()

    def test_pulls_lock_timeout_and_corrupt_store(self):
        self.setup_pr()
        lock_path = pulls.pulls_path(self.repo) + ".lock"
        with open(lock_path, "wb"):
            pass
        try:
            with mock.patch.object(pulls, "PULLS_LOCK_TIMEOUT", 0.05):
                with self.assertRaises(MinigitError) as cm:
                    pulls.close_pull(self.repo, "alice", 1)
                self.assertIn("unable to lock", cm.exception.message)
        finally:
            os.unlink(lock_path)
        with open(pulls.pulls_path(self.repo), "wb") as fh:
            fh.write(b"{not json")
        with self.assertRaises(MinigitError):
            pulls.load_pulls(self.repo)

    def test_validation(self):
        for bad in (None, "", "  ", 5, "a\nb", "x" * 257):
            with self.subTest(title=bad):
                self.assert_pr_error(400, "bad_request", pulls.validate_title, bad)
        self.assertEqual(pulls.validate_title("x" * 256), "x" * 256)
        self.assert_pr_error(400, "bad_request", pulls.validate_body, "x" * (pulls.BODY_MAX + 1))
        self.assert_pr_error(400, "bad_request", pulls.validate_state_filter, "bogus")
        for text in ("0", "01", "abc", "-1", "1234567890"):
            with self.subTest(number=text):
                self.assert_pr_error(404, "not_found", pulls.parse_number, text)


class TestPullsHttp(PullsTestCase):
    def test_body_limit_head_request_and_errors(self):
        self.setup_pr()
        big = b'{"title": "' + b"x" * (pulls.PULL_JSON_MAX + 1) + b'"}'
        status, _, body = self.request("POST", "/proj/pulls", big, content_type="application/json")
        self.assertEqual((status, json.loads(body)["code"]), (413, "too_large"))
        status, headers, body = self.request("HEAD", "/proj/pulls/1")
        self.assertEqual((status, body), (200, b""))
        status, _, body = self.request("POST", "/proj/pulls/1/merge", b"", user="carol")
        self.assertEqual((status, json.loads(body)["code"]), (403, "forbidden"))
        status, _, body = self.request("GET", "/proj/pulls?state=open&state=all")
        self.assertEqual((status, json.loads(body)["code"]), (400, "bad_request"))
        status, _, body = self.request("POST", "/proj/pulls/1/close", b"")  # an empty body is {}
        self.assertEqual((status, json.loads(body)["pull"]["state"]), (200, "closed"))

    def test_merge_over_http_audits_and_corrupt_store_is_500(self):
        root, head = self.setup_pr()
        status, _, body = self.request("POST", "/proj/pulls/1/merge", b"{}", content_type="application/json")
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        with open(os.path.join(self.root, auth.AUDIT_LOG), "rb") as fh:
            lines = fh.read().decode().splitlines()
        self.assertTrue(lines[-1].endswith(f" alice proj refs/heads/main {root} {result['new']} ok"), lines)
        with open(pulls.pulls_path(self.repo), "wb") as fh:
            fh.write(b"[]")
        status, _, body = self.request("GET", "/proj/pulls")
        self.assertEqual((status, json.loads(body)["code"]), (500, "internal"))

    def test_private_repo_hides_numbers_from_anonymous(self):
        auth.create_repo(self.root, "secret", readers=["alice"], writers=["alice"])
        status, headers, _ = self.request("GET", "/secret/pulls/99", user=None)
        self.assertEqual(status, 401)
        status, _, body = self.request("GET", "/secret/pulls/99")
        self.assertEqual((status, json.loads(body)["code"]), (404, "not_found"))
        self.assertIn("#99 not found", json.loads(body)["error"])
        status, _, _ = self.request("GET", "/secret/pulls/abc", user=None)
        self.assertEqual(status, 401, "an unreadable repo is a 401 before any routing")


if __name__ == "__main__":
    unittest.main()

"""tests.test_diff_fixes -- regression tests for diff speed and output fixes."""
from __future__ import annotations

import os
import tempfile
import time
import unittest

from minigit import diff as diffmod
from minigit.index import IndexEntry, locked_index
from minigit.objects import hash_object
from minigit.repo import Repository
from tests.helpers import make_env, run, write


class TimingTest(unittest.TestCase):
    def test_identical_lines_plus_one(self):
        t = time.time()
        out = diffmod.render_hunks(b"\n" * 8000, b"\n" * 8001)
        self.assertLess(time.time() - t, 1.0)
        self.assertIn(b"+\n", out)

    def test_repetitive_file_with_scattered_edits(self):
        a = b"ab\n" * 13500
        b = bytearray(a)
        for p in (1500, 9000, 21000, 30000, 39000):
            b[p:p] = b"zz\n"
        t = time.time()
        out = diffmod.render_hunks(a, bytes(b))
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(out.count(b"@@ -"), 5)
        self.assertEqual(out.count(b"\n+zz\n"), 5)

    def test_all_different_large(self):
        a = b"".join(b"%d\n" % i for i in range(5000))
        b = b"".join(b"x%d\n" % i for i in range(5000))
        t = time.time()
        diffmod.render_hunks(a, b)
        self.assertLess(time.time() - t, 3.0)


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.wt = os.path.join(os.path.realpath(self._tmp.name), "wt")
        os.makedirs(self.wt)
        self.env = make_env(os.path.realpath(self._tmp.name))
        self.m("init")

    def m(self, *a, check=True):
        return run(a, self.wt, env=self.env, check=check)

    def test_empty_file_has_only_header_lines(self):
        write(os.path.join(self.wt, "e"), b"")
        self.m("add", "e")
        out = self.m("diff", "--cached").out
        self.assertEqual(out, "diff --git a/e b/e\nnew file mode 100644\nindex 0000000..e69de29\n")
        self.m("commit", "-m", "c")
        os.remove(os.path.join(self.wt, "e"))
        out = self.m("diff").out
        self.assertEqual(out, "diff --git a/e b/e\ndeleted file mode 100644\nindex e69de29..0000000\n")

    def test_cached_with_revision(self):
        write(os.path.join(self.wt, "f"), "1\n")
        self.m("add", "f")
        self.m("commit", "-m", "one")
        write(os.path.join(self.wt, "f"), "2\n")
        self.m("add", "f")
        self.m("commit", "-m", "two")
        out = self.m("diff", "--cached", "HEAD~1").out
        self.assertIn("-1\n+2\n", out)
        self.assertEqual(self.m("diff", "--staged", "HEAD").out, "")

    def test_binary_add_and_delete(self):
        write(os.path.join(self.wt, "x"), b"\0\1\2")
        self.m("add", "x")
        self.assertIn("Binary files /dev/null and b/x differ", self.m("diff", "--cached").out)
        self.m("commit", "-m", "c")
        os.remove(os.path.join(self.wt, "x"))
        self.assertIn("Binary files a/x and /dev/null differ", self.m("diff").out)

    def test_diff_shows_conflict_markers(self):
        f = os.path.join(self.wt, "f.txt")
        write(f, "a\nb\nc\n")
        self.m("add", "f.txt")
        self.m("commit", "-m", "base")
        self.m("checkout", "-b", "feat")
        write(f, "a\nfeat\nc\n")
        self.m("commit", "-a", "-m", "feat")
        self.m("checkout", "-")  if False else self.m("checkout", "main")
        write(f, "a\nmain\nc\n")
        self.m("commit", "-a", "-m", "main")
        self.m("merge", "feat", check=False)
        out = self.m("diff").out
        self.assertIn("* Unmerged path f.txt", out)
        self.assertIn("+<<<<<<<", out)
        self.assertIn("+>>>>>>>", out)
        self.assertIn("@@", out)

    def test_diff_rev_keeps_revision_mode_for_path_missing_from_index(self):
        write(os.path.join(self.wt, "run.sh"), "echo hi\n")
        self.m("add", "run.sh")
        self.m("commit", "-m", "c1")
        with locked_index(Repository.open(self.wt)) as index:
            e = index.get("run.sh")
            index.put(IndexEntry("run.sh", "100755", e.sha, e.size, e.mtime_ns, 0))
        self.m("commit", "-m", "make executable")
        self.m("rm", "--cached", "run.sh")
        out = self.m("diff", "HEAD").out
        if os.name == "nt":   # no exec bit on disk: the mode comes from HEAD, so nothing changed
            self.assertEqual(out, "")
        else:                 # the file on disk is not executable
            self.assertEqual(out, "diff --git a/run.sh b/run.sh\nold mode 100755\nnew mode 100644\n")

    def test_unmerged_path_without_stage_2_prints_no_diff(self):
        x = hash_object("blob", b"x\n")
        y = hash_object("blob", b"y\n")
        repo = Repository.open(self.wt)
        repo.objects.write("blob", b"x\n")
        repo.objects.write("blob", b"y\n")
        with locked_index(repo) as index:
            index.put(IndexEntry("au.txt", "100644", x, 0, 0, 2))
            index.put(IndexEntry("ua.txt", "100644", y, 0, 0, 3))
        for n in ("au.txt", "ua.txt"):
            write(os.path.join(self.wt, n), "x\n")
        # like `git diff --ours`: compared with stage 2 only; ua.txt has none, so no diff of stage 3
        self.assertEqual(self.m("diff").out, "* Unmerged path au.txt\n* Unmerged path ua.txt\n")
        write(os.path.join(self.wt, "au.txt"), "x\nmore\n")
        self.assertIn("* Unmerged path au.txt\ndiff --git a/au.txt b/au.txt\n", self.m("diff").out)


if __name__ == "__main__":
    unittest.main()

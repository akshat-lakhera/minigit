"""End-to-end tests for the local workflow (owner: M7 cli).

Every step runs the real CLI (``python -m minigit``) in a temporary
directory: init, add, status, commit, log, diff, rm, ignore, branching,
merge conflict, resolve, abort, reset, restore and a detached HEAD.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest

from tests.helpers import ROOT, make_env, read, run, write

HEX7 = r"[0-9a-f]{7}"
HEX40 = re.compile(r"^[0-9a-f]{40}$")


class LocalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.env = make_env(self.root)
        self.wt = os.path.join(self.root, "wt")
        os.makedirs(self.wt)

    # -- helpers ----------------------------------------------------------
    def mg(self, *args, cwd=None, check=True, env=None):
        return run(args, cwd=cwd or self.wt, env=env or self.env, check=check)

    def fail_mg(self, *args, code=1, cwd=None, env=None):
        """Run a command that must fail with ``code``; return the result."""
        res = self.mg(*args, cwd=cwd, check=False, env=env)
        self.assertEqual(res.returncode, code, str(res))
        return res

    def write(self, rel, data):
        write(os.path.join(self.wt, rel), data)

    def read(self, rel):
        return read(os.path.join(self.wt, rel))

    def exists(self, rel):
        return os.path.lexists(os.path.join(self.wt, rel))

    def gitfile(self, name):
        return read(os.path.join(self.wt, ".minigit", name)).decode()

    def init(self):
        self.mg("init")

    def commit(self, rel, data, msg):
        self.write(rel, data)
        self.mg("add", rel)
        self.mg("commit", "-m", msg)
        return self.head()

    def head(self, cwd=None):
        sha = self.mg("rev-parse", "HEAD", cwd=cwd).out.strip()
        self.assertRegex(sha, HEX40)
        return sha

    def porcelain(self):
        return self.mg("status", "--porcelain").out

    def oneline(self):
        return self.mg("log", "--oneline").out.splitlines()


class InitAddCommitLogTests(LocalBase):
    def test_init_add_status_commit_log(self):
        res = self.mg("init")
        self.assertIn("Initialized empty minigit repository", res.out)
        self.assertEqual(self.gitfile("HEAD"), "ref: refs/heads/main\n")
        self.assertIn("Reinitialized", self.mg("init").out)

        status = self.mg("status").out
        self.assertIn("On branch main", status)
        self.assertIn("No commits yet", status)
        self.assertIn("nothing to commit", status)

        self.write("a.txt", "hello\n")
        self.write("sub/b.txt", "b\n")
        self.assertEqual(self.porcelain(), "?? a.txt\n?? sub/b.txt\n")
        status = self.mg("status").out
        self.assertIn("Untracked files:", status)
        self.assertIn("\ta.txt", status)

        self.mg("add", "a.txt")
        self.assertEqual(self.porcelain(), "A  a.txt\n?? sub/b.txt\n")
        status = self.mg("status").out
        self.assertIn("Changes to be committed:", status)
        self.assertIn("\tnew file:   a.txt", status)

        res = self.fail_mg("commit", code=2)
        self.assertIn("commit message required", res.err)

        res = self.mg("commit", "-m", "first")
        self.assertRegex(res.out, rf"^\[main \(root-commit\) {HEX7}\] first\n$")
        first = self.head()

        self.mg("add", ".")
        self.assertEqual(self.porcelain(), "A  sub/b.txt\n")
        res = self.mg("commit", "-m", "second")
        self.assertRegex(res.out, rf"^\[main {HEX7}\] second\n$")
        second = self.head()
        self.assertNotEqual(first, second)

        lines = self.oneline()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], rf"^{HEX7} \(HEAD -> main\) second$")
        self.assertRegex(lines[1], rf"^{HEX7} first$")
        self.assertEqual(len(self.mg("log", "--oneline", "-n", "1").out.splitlines()), 1)

        full = self.mg("log").out
        self.assertIn(f"commit {second} (HEAD -> main)", full)
        self.assertIn(f"commit {first}", full)
        self.assertIn("Author: Test User <test@example.com>", full)
        self.assertIn("Date:   ", full)
        self.assertIn("\n    second\n", full)
        self.assertIn("\n    first\n", full)

        self.assertIn("nothing to commit, working tree clean", self.mg("status").out)
        self.assertEqual(self.porcelain(), "")
        res = self.fail_mg("commit", "-m", "empty")
        self.assertIn("nothing to commit", res.err)
        self.assertRegex(self.mg("commit", "--allow-empty", "-m", "empty ok").out, rf"^\[main {HEX7}\] empty ok\n$")

        self.assertEqual(self.mg("cat-file", "-t", "HEAD").out.strip(), "commit")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:a.txt").stdout, b"hello\n")
        self.assertEqual(self.mg("ls-files").out, "a.txt\nsub/b.txt\n")
        self.assertEqual(self.mg("rev-parse", "HEAD~1").out.strip(), second)

    def test_identity_from_config(self):
        env = dict(self.env)
        for key in ("MINIGIT_AUTHOR_NAME", "MINIGIT_AUTHOR_EMAIL"):
            env.pop(key)
        self.mg("init", env=env)
        self.write("a.txt", "x\n")
        self.mg("add", "a.txt", env=env)
        res = self.fail_mg("commit", "-m", "x", env=env)
        self.assertIn("please set your identity", res.err)
        self.mg("config", "--global", "user.name", "Config User", env=env)
        self.mg("config", "--global", "user.email", "config@example.com", env=env)
        self.assertEqual(self.mg("config", "user.name", env=env).out.strip(), "Config User")
        self.mg("commit", "-m", "x", env=env)
        self.assertIn("Author: Config User <config@example.com>", self.mg("log", env=env).out)
        self.mg("config", "user.name", "Local User", env=env)
        self.assertEqual(self.mg("config", "user.name", env=env).out.strip(), "Local User")
        self.assertIn("user.name=Local User", self.mg("config", "--list", env=env).out)
        self.mg("config", "--unset", "user.name", env=env)
        self.assertEqual(self.mg("config", "user.name", env=env).out.strip(), "Config User")

    def test_outside_repository_and_usage(self):
        res = self.fail_mg("status")
        self.assertIn("not a minigit repository", res.err)
        self.init()
        res = self.fail_mg("log")
        self.assertIn("does not have any commits yet", res.err)
        res = self.fail_mg("frobnicate", code=2)
        self.assertIn("error:", res.err)
        self.assertIn("minigit 0.1.0", self.mg("--version").out)
        self.assertIn("usage: minigit commit", self.mg("commit", "--help").out)
        res = self.fail_mg("add", "nope")
        self.assertIn("pathspec 'nope' did not match any files", res.err)

    def test_entry_scripts(self):
        env = dict(self.env)
        script = os.path.join(ROOT, "bin", "minigit")
        proc = subprocess.run([sys.executable, script, "--version"], capture_output=True, timeout=30,
                              env=env, cwd=self.wt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"minigit 0.1.0", proc.stdout)
        proc = subprocess.run([sys.executable, script, "init"], capture_output=True, timeout=30,
                              env=env, cwd=self.wt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.wt, ".minigit")))
        if os.name == "nt":
            cmd = os.path.join(ROOT, "bin", "minigit.cmd")
            proc = subprocess.run([cmd, "--version"], capture_output=True, timeout=30, env=env,
                                  cwd=self.wt, shell=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(b"minigit 0.1.0", proc.stdout)


class DiffTests(LocalBase):
    def test_diff_forms(self):
        self.init()
        self.commit("a.txt", "line1\nline2\nline3\n", "base")
        self.assertEqual(self.mg("diff").out, "")
        self.write("a.txt", "line1\nchanged\nline3\n")
        out = self.mg("diff").out
        self.assertIn("diff --git a/a.txt b/a.txt\n", out)
        self.assertIn("--- a/a.txt\n+++ b/a.txt\n", out)
        self.assertIn("@@ -1,3 +1,3 @@\n", out)
        self.assertIn("-line2\n+changed\n", out)
        self.assertEqual(self.mg("diff", "--staged").out, "")

        self.mg("add", "a.txt")
        self.assertEqual(self.mg("diff").out, "")
        self.assertIn("+changed", self.mg("diff", "--staged").out)
        self.assertIn("+changed", self.mg("diff", "--cached").out)
        self.mg("commit", "-m", "change")

        for args in (["HEAD~1", "HEAD"], ["HEAD~1..HEAD"], ["HEAD~1"], ["HEAD~1", "--", "a.txt"]):
            out = self.mg("diff", *args).out
            self.assertIn("-line2\n+changed\n", out, args)
        self.assertEqual(self.mg("diff", "HEAD~1", "--", "other").out, "")
        self.assertEqual(self.mg("diff", "HEAD", "HEAD~1").out.count("+line2"), 1)

        self.write("c.txt", "no newline")
        self.mg("add", "c.txt")
        out = self.mg("diff", "--staged").out
        self.assertIn("new file mode 100644\n", out)
        self.assertIn("--- /dev/null\n+++ b/c.txt\n", out)
        self.assertIn("+no newline\n\\ No newline at end of file\n", out)

        self.write("bin.dat", b"\x00\x01\x02")
        self.mg("add", "bin.dat")
        out = self.mg("diff", "--staged", "--", "bin.dat").out
        self.assertIn("Binary files", out)
        self.assertIn("differ", out)
        self.assertNotIn("+++", out)
        self.mg("commit", "-m", "more")

        show = self.mg("show", "HEAD~1").out
        self.assertIn("commit ", show)
        self.assertIn("    change\n", show)
        self.assertIn("-line2\n+changed\n", show)
        self.assertEqual(self.mg("show", "HEAD:a.txt").stdout, b"line1\nchanged\nline3\n")

    def test_crlf_and_bytes_preserved(self):
        self.init()
        data = b"a\r\nb\r\n"
        self.commit("crlf.txt", data, "crlf")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:crlf.txt").stdout, data)
        self.write("crlf.txt", b"a\r\nB\r\n")
        out = self.mg("diff").stdout
        self.assertIn(b"-b\r\n+B\r\n", out)
        self.mg("restore", "crlf.txt")
        self.assertEqual(self.read("crlf.txt"), data)
        self.assertEqual(self.porcelain(), "")


class RmIgnoreTests(LocalBase):
    def test_rm(self):
        self.init()
        for rel in ("a.txt", "b.txt", "d.txt", "sub/c.txt"):
            self.write(rel, rel + "\n")
        self.mg("add", ".")
        self.mg("commit", "-m", "base")

        self.assertEqual(self.mg("rm", "a.txt").out, "rm 'a.txt'\n")
        self.assertFalse(self.exists("a.txt"))
        self.assertEqual(self.porcelain(), "D  a.txt\n")
        self.assertIn("\tdeleted:    a.txt", self.mg("status").out)
        self.mg("commit", "-m", "rm a")

        self.write("b.txt", "modified\n")
        res = self.fail_mg("rm", "b.txt")
        self.assertIn("has staged or local changes", res.err)
        self.assertTrue(self.exists("b.txt"))
        self.mg("rm", "-f", "b.txt")
        self.assertFalse(self.exists("b.txt"))

        self.fail_mg("rm", "sub")
        self.assertTrue(self.exists("sub/c.txt"))
        self.mg("rm", "-r", "sub")
        self.assertFalse(self.exists("sub"))

        self.mg("rm", "--cached", "d.txt")
        self.assertTrue(self.exists("d.txt"))
        # entries are sorted by (path, XY): "??" sorts before "D "
        self.assertEqual(self.porcelain(), "D  b.txt\n?? d.txt\nD  d.txt\nD  sub/c.txt\n")
        self.fail_mg("rm", "nope")

    def test_ignore(self):
        self.init()
        self.write(".minigitignore", "*.log\nbuild/\n!keep.log\n")
        self.write("x.log", "x")
        self.write("keep.log", "k")
        self.write("build/out.txt", "o")
        self.write("keep.txt", "k")
        self.write("sub/.minigitignore", "secret\n")
        self.write("sub/secret", "s")
        self.write("sub/visible", "v")
        self.write("sub/deep/also.log", "l")
        self.assertEqual(self.porcelain(), "?? .minigitignore\n?? keep.log\n?? keep.txt\n"
                                           "?? sub/.minigitignore\n?? sub/visible\n")
        self.mg("add", ".")
        self.assertEqual(self.mg("ls-files").out, ".minigitignore\nkeep.log\nkeep.txt\n"
                                                   "sub/.minigitignore\nsub/visible\n")
        res = self.fail_mg("add", "x.log")
        self.assertIn("ignored by .minigitignore (use -f)", res.err)
        self.mg("add", "-f", "x.log")
        self.assertIn("x.log\n", self.mg("ls-files").out)
        self.mg("commit", "-m", "base")
        self.write("x.log", "changed")
        self.assertEqual(self.porcelain(), " M x.log\n")
        self.mg("add", ".")
        self.assertEqual(self.porcelain(), "M  x.log\n")
        os.remove(os.path.join(self.wt, "keep.txt"))
        self.mg("add", ".")
        self.assertIn("D  keep.txt\n", self.porcelain())


class BranchMergeTests(LocalBase):
    def test_branch_switch_fast_forward_and_three_way(self):
        self.init()
        self.commit("a.txt", "base\n", "c1")
        self.assertEqual(self.mg("branch").out, "* main\n")
        self.mg("branch", "feature")
        self.assertEqual(self.mg("branch").out, "  feature\n* main\n")
        res = self.fail_mg("branch", "feature")
        self.assertIn("a branch named 'feature' already exists", res.err)
        self.assertRegex(self.mg("branch", "-v").out, rf"\* main +{HEX7} c1")

        self.assertEqual(self.mg("switch", "feature").out, "Switched to branch 'feature'\n")
        self.assertIn("On branch feature", self.mg("status").out)
        self.commit("a.txt", "feature\n", "feature change")
        self.mg("switch", "main")
        self.assertEqual(self.read("a.txt"), b"base\n")

        res = self.mg("merge", "feature")
        self.assertRegex(res.out, rf"Updating {HEX7}\.\.{HEX7}\nFast-forward")
        self.assertEqual(self.read("a.txt"), b"feature\n")
        self.assertEqual(len(self.oneline()), 2)
        self.assertEqual(self.mg("merge", "feature").out, "Already up to date.\n")

        self.assertIn("Switched to a new branch 'topic'", self.mg("switch", "-c", "topic").out)
        self.commit("t.txt", "t\n", "t")
        self.mg("switch", "main")
        self.assertFalse(self.exists("t.txt"))
        self.commit("m.txt", "m\n", "m")
        res = self.mg("merge", "topic")
        self.assertIn("Merge made by the 'three-way' strategy.", res.out)
        self.assertTrue(self.exists("t.txt") and self.exists("m.txt"))
        self.assertIn("Merge: ", self.mg("log", "-n", "1").out)
        self.assertIn("Merge branch 'topic'", self.oneline()[0])
        self.assertEqual(self.porcelain(), "")

        self.assertRegex(self.mg("branch", "-d", "topic").out, rf"Deleted branch topic \(was {HEX7}\)\.")
        res = self.fail_mg("branch", "-d", "main")
        self.assertIn("cannot delete the branch 'main' you are on", res.err)

        self.mg("switch", "-c", "wip")
        self.commit("w.txt", "w\n", "w")
        self.mg("switch", "main")
        res = self.fail_mg("branch", "-d", "wip")
        self.assertIn("not fully merged", res.err)
        self.mg("branch", "-D", "wip")
        self.assertNotIn("wip", self.mg("branch").out)

        self.mg("switch", "-c", "noff")
        self.commit("n.txt", "n\n", "n")
        self.mg("switch", "main")
        self.assertIn("Merge made by the 'three-way' strategy.", self.mg("merge", "--no-ff", "noff").out)
        self.assertIn("Merge: ", self.mg("log", "-n", "1").out)
        res = self.fail_mg("switch", "nope")
        self.assertIn("error:", res.err)

    def test_switch_refuses_to_clobber(self):
        self.init()
        self.commit("a.txt", "base\n", "c1")
        self.mg("switch", "-c", "other")
        self.commit("a.txt", "other\n", "other a")
        self.mg("switch", "main")
        self.write("a.txt", "uncommitted\n")
        res = self.fail_mg("switch", "other")
        self.assertIn("would be overwritten by checkout", res.err)
        self.assertIn("\ta.txt", res.err)
        self.assertEqual(self.read("a.txt"), b"uncommitted\n")
        self.assertIn("On branch main", self.mg("status").out)
        self.mg("restore", "a.txt")
        self.mg("switch", "other")
        self.assertEqual(self.read("a.txt"), b"other\n")
        self.write("new.txt", "carried\n")
        self.mg("switch", "main")
        self.assertEqual(self.read("new.txt"), b"carried\n")
        self.assertEqual(self.read("a.txt"), b"base\n")

    def test_merge_conflict_resolve_and_abort(self):
        self.init()
        self.commit("a.txt", "one\ntwo\nthree\n", "base")
        self.commit("other.txt", "clean\n", "other")
        self.mg("branch", "feature")
        self.commit("a.txt", "one\nMAIN\nthree\n", "main change")
        self.mg("switch", "feature")
        self.commit("a.txt", "one\nFEATURE\nthree\n", "feature change")
        self.mg("switch", "main")
        self.write("other.txt", "dirty\n")

        res = self.fail_mg("merge", "feature")
        self.assertIn("CONFLICT (content): Merge conflict in a.txt", res.out)
        self.assertIn("Automatic merge failed; fix conflicts and then commit the result.", res.out)
        self.assertEqual(self.read("a.txt"),
                         b"one\n<<<<<<< HEAD\nMAIN\n=======\nFEATURE\n>>>>>>> feature\nthree\n")
        self.assertTrue(self.exists(".minigit/MERGE_HEAD"))
        status = self.mg("status").out
        self.assertIn("You have unmerged paths.", status)
        self.assertIn("\tboth modified:   a.txt", status)
        self.assertIn("UU a.txt\n", self.porcelain())
        self.assertIn("* Unmerged path a.txt", self.mg("diff").out)
        res = self.fail_mg("commit", "-m", "x")
        self.assertIn("unresolved conflicts", res.err)
        res = self.fail_mg("switch", "feature")
        self.assertIn("merge in progress", res.err)

        self.mg("merge", "--abort")
        self.assertEqual(self.read("a.txt"), b"one\nMAIN\nthree\n")
        self.assertFalse(self.exists(".minigit/MERGE_HEAD"))
        self.assertEqual(self.read("other.txt"), b"dirty\n")
        self.assertEqual(self.porcelain(), " M other.txt\n")
        res = self.fail_mg("merge", "--abort")
        self.assertIn("no merge to abort", res.err)

        self.fail_mg("merge", "feature")
        self.write("a.txt", "one\nRESOLVED\nthree\n")
        self.mg("add", "a.txt")
        self.assertIn("All conflicts fixed but you are still merging.", self.mg("status").out)
        res = self.mg("commit")
        self.assertRegex(res.out, rf"^\[main {HEX7}\] Merge branch 'feature'\n$")
        self.assertFalse(self.exists(".minigit/MERGE_HEAD"))
        self.assertEqual(self.read("a.txt"), b"one\nRESOLVED\nthree\n")
        self.assertIn("Merge: ", self.mg("log", "-n", "1").out)
        self.assertEqual(len(self.oneline()), 5)
        self.assertEqual(self.porcelain(), " M other.txt\n")

    def test_modify_delete_conflict(self):
        self.init()
        self.commit("a.txt", "x\n", "base")
        self.mg("branch", "feature")
        self.mg("rm", "a.txt")
        self.mg("commit", "-m", "delete a")
        self.mg("switch", "feature")
        self.commit("a.txt", "y\n", "modify a")
        self.mg("switch", "main")
        res = self.fail_mg("merge", "feature")
        self.assertIn("CONFLICT (modify/delete): a.txt", res.out)
        self.assertEqual(self.read("a.txt"), b"y\n")
        self.mg("merge", "--abort")
        self.assertFalse(self.exists("a.txt"))
        self.assertEqual(self.porcelain(), "")


class ResetRestoreDetachedTests(LocalBase):
    def test_reset_and_restore(self):
        self.init()
        c1 = self.commit("a.txt", "v1\n", "c1")
        c2 = self.commit("a.txt", "v2\n", "c2")
        c3 = self.commit("a.txt", "v3\n", "c3")

        self.mg("reset", "--soft", "HEAD~1")
        self.assertEqual(self.head(), c2)
        self.assertEqual(self.gitfile("ORIG_HEAD").strip(), c3)
        self.assertEqual(self.porcelain(), "M  a.txt\n")
        self.assertEqual(self.read("a.txt"), b"v3\n")

        self.mg("reset")
        self.assertEqual(self.porcelain(), " M a.txt\n")
        res = self.mg("reset", "--hard")
        self.assertRegex(res.out, rf"HEAD is now at {HEX7} c2")
        self.assertEqual(self.read("a.txt"), b"v2\n")
        self.assertEqual(self.porcelain(), "")

        self.mg("reset", "--hard", c3)
        self.assertEqual(self.read("a.txt"), b"v3\n")
        self.mg("reset", "--mixed", "HEAD~2")
        self.assertEqual(self.head(), c1)
        self.assertEqual(self.read("a.txt"), b"v3\n")
        self.assertEqual(self.porcelain(), " M a.txt\n")
        self.write("untracked.txt", "u\n")
        self.mg("reset", "--hard", c3)
        self.assertEqual(self.read("a.txt"), b"v3\n")
        self.assertEqual(self.read("untracked.txt"), b"u\n")

        self.write("a.txt", "dirty\n")
        self.mg("restore", "a.txt")
        self.assertEqual(self.read("a.txt"), b"v3\n")
        self.write("a.txt", "staged\n")
        self.mg("add", "a.txt")
        self.assertEqual(self.porcelain(), "M  a.txt\n?? untracked.txt\n")
        self.mg("restore", "--staged", "a.txt")
        self.assertEqual(self.porcelain(), " M a.txt\n?? untracked.txt\n")
        self.mg("restore", "a.txt")
        self.assertEqual(self.porcelain(), "?? untracked.txt\n")
        self.mg("restore", "--source", "HEAD~1", "a.txt")
        self.assertEqual(self.read("a.txt"), b"v2\n")
        self.assertEqual(self.porcelain(), " M a.txt\n?? untracked.txt\n")
        self.mg("restore", "--staged", "--worktree", "--source", "HEAD", "a.txt")
        self.assertEqual(self.read("a.txt"), b"v3\n")
        res = self.fail_mg("restore", "nope")
        self.assertIn("did not match", res.err)

        self.mg("checkout", "HEAD~1", "--", "a.txt")
        self.assertEqual(self.read("a.txt"), b"v2\n")
        self.assertEqual(self.porcelain(), "M  a.txt\n?? untracked.txt\n")
        self.mg("reset", "--", "a.txt")
        self.assertEqual(self.porcelain(), " M a.txt\n?? untracked.txt\n")
        self.mg("checkout", "--", "a.txt")
        self.assertEqual(self.read("a.txt"), b"v3\n")
        self.assertEqual(self.porcelain(), "?? untracked.txt\n")

    def test_detached_head(self):
        self.init()
        c1 = self.commit("a.txt", "v1\n", "c1")
        c2 = self.commit("a.txt", "v2\n", "c2")
        res = self.mg("checkout", c1)
        self.assertIn("HEAD is now at", res.out)
        self.assertEqual(self.gitfile("HEAD"), c1 + "\n")
        self.assertIn(f"HEAD detached at {c1[:7]}", self.mg("status").out)
        self.assertEqual(self.read("a.txt"), b"v1\n")
        self.assertEqual(len(self.oneline()), 1)
        self.assertIn("(HEAD)", self.oneline()[0])

        d = self.commit("d.txt", "d\n", "detached commit")
        self.assertRegex(self.mg("log", "-n", "1", "--oneline").out, rf"^{HEX7} \(HEAD\) detached commit")
        res = self.mg("switch", "main")
        self.assertEqual(res.out, "Switched to branch 'main'\n")
        self.assertEqual(self.head(), c2)
        self.assertFalse(self.exists("d.txt"))
        self.assertEqual(self.gitfile("HEAD"), "ref: refs/heads/main\n")

        self.mg("switch", "--detach", "HEAD~1")
        self.assertIn("HEAD detached at", self.mg("status").out)
        self.assertEqual(self.head(), c1)
        self.assertIn("Switched to a new branch 'fromdetached'", self.mg("switch", "-c", "fromdetached").out)
        self.assertIn("On branch fromdetached", self.mg("status").out)
        self.mg("branch", "saved", d)
        self.assertIn("  saved\n", self.mg("branch").out)
        self.mg("checkout", "main")
        self.assertIn("On branch main", self.mg("status").out)
        self.assertEqual(len(self.mg("log", "--oneline", "--all").out.splitlines()), 3)

    def test_switch_create_on_unborn(self):
        self.init()
        self.mg("switch", "-c", "dev")
        self.assertEqual(self.gitfile("HEAD"), "ref: refs/heads/dev\n")
        res = self.commit("a.txt", "a\n", "root")
        self.assertRegex(self.mg("log", "--oneline").out, rf"^{HEX7} \(HEAD -> dev\) root\n$")
        self.assertRegex(res, HEX40)


if __name__ == "__main__":
    unittest.main()

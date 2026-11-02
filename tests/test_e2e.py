"""End-to-end tests for the integrated minigit (owner: integration).

Every step runs the real command line as a subprocess (``python -m minigit``,
and the ``bin/minigit`` entry scripts) in temporary directories:

* ``SingleUserWorkflowTests``: init, add, commit, status, diff, log, show, rm,
  ignore rules, branch, checkout/switch (refusing to clobber local changes),
  fast-forward, three-way and conflicting merges (abort and resolve), reset
  --soft/--mixed/--hard, restore and a detached HEAD.
* ``MultiUserWorkflowTests``: a live ``minigit serve`` on a free localhost
  port. Alice and bob clone the same repository and both commit; bob's push is
  rejected until he pulls and merges; a conflicting edit is resolved; a second
  repository is cloned; bad tokens and missing write access are refused; and
  concurrent pushes leave exactly one winner with a consistent server.
* ``EntryPointTests``: ``--help``/``--version``, exit codes, and the
  ``bin/minigit`` and ``bin\\minigit.cmd`` entry scripts.

The server is started in ``setUpClass`` and always stopped (class cleanups
run even when set-up fails) before its temporary directory is removed.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from tests.helpers import ROOT, ServerProcess, make_env, read, run, write

HEX7 = r"[0-9a-f]{7}"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
ONELINE_PREFIX = re.compile(r"^[0-9a-f]{7}(?: \([^)]*\))? ")


class CliCase(unittest.TestCase):
    """Helpers shared by the end-to-end tests. ``self.env`` is the default environment."""

    env: dict

    def make_tmp(self) -> str:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        return os.path.realpath(tmp.name)

    def mg(self, cwd, *args, env=None):
        """Run ``minigit *args`` in ``cwd``; it must exit 0."""
        return run(args, cwd=cwd, env=env or self.env)

    def mg_fail(self, cwd, *args, env=None, code=1):
        """Run ``minigit *args`` in ``cwd``; it must exit with ``code``."""
        res = run(args, cwd=cwd, env=env or self.env, check=False)
        self.assertEqual(res.returncode, code, str(res))
        return res

    def porcelain(self, cwd, env=None) -> list[str]:
        return self.mg(cwd, "status", "--porcelain", env=env).out.splitlines()

    def rev(self, cwd, spec, env=None) -> str:
        sha = self.mg(cwd, "rev-parse", spec, env=env).out.strip()
        self.assertRegex(sha, HEX40)
        return sha

    def head(self, cwd, env=None) -> str:
        return self.rev(cwd, "HEAD", env)

    def subjects(self, cwd, env=None) -> list[str]:
        """Commit subjects from ``log --oneline``, newest first."""
        lines = self.mg(cwd, "log", "--oneline", env=env).out.splitlines()
        return [ONELINE_PREFIX.sub("", line) for line in lines]


# ---------------------------------------------------------------------------
# One user, one repository
# ---------------------------------------------------------------------------

class SingleUserWorkflowTests(CliCase):
    def setUp(self):
        self.tmp = self.make_tmp()
        self.env = make_env(self.tmp)
        self.repo = os.path.join(self.tmp, "proj")

    # -- helpers ----------------------------------------------------------
    def git(self, *args):
        return self.mg(self.repo, *args)

    def git_fail(self, *args, code=1):
        return self.mg_fail(self.repo, *args, code=code)

    def put(self, rel, data):
        write(os.path.join(self.repo, rel), data)

    def get(self, rel) -> bytes:
        return read(os.path.join(self.repo, rel))

    def exists(self, rel) -> bool:
        return os.path.lexists(os.path.join(self.repo, rel))

    def commit_file(self, rel, data, msg) -> str:
        self.put(rel, data)
        self.git("add", rel)
        self.git("commit", "-m", msg)
        return self.head(self.repo)

    def new_repo(self):
        self.mg(self.tmp, "init", "proj")

    # -- tests ------------------------------------------------------------
    def test_init_add_commit_status_diff_log_show_rm(self):
        res = self.mg(self.tmp, "init", "proj")
        self.assertIn("Initialized empty minigit repository in", res.out)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, ".minigit", "objects")))
        self.assertIn("Reinitialized existing minigit repository", self.git("init").out)
        status = self.git("status").out
        self.assertIn("On branch main", status)
        self.assertIn("No commits yet", status)
        self.assertIn("does not have any commits yet", self.git_fail("log").err)

        self.put("README.md", "hello\n")
        self.put("src/app.py", "print('hi')\n")
        self.put("src/lib/util.py", b"x = 1\r\ny = 2\r\n")  # CRLF must survive byte for byte
        self.put(".minigitignore", "*.log\nbuild/\n")
        self.put("debug.log", "noise\n")
        self.put("build/out.bin", b"\x00\x01\x02")
        self.assertEqual(self.porcelain(self.repo), [
            "?? .minigitignore", "?? README.md", "?? src/app.py", "?? src/lib/util.py"])

        self.git("add", "README.md")  # a file
        self.git("add", "src")        # a directory
        self.assertEqual(self.porcelain(self.repo), [
            "?? .minigitignore", "A  README.md", "A  src/app.py", "A  src/lib/util.py"])
        self.assertIn("ignored", self.git_fail("add", "debug.log").err)
        self.assertIn("did not match any files", self.git_fail("add", "nope.txt", code=128).err)
        self.git("add", ".")          # everything that is not ignored
        self.assertEqual(self.porcelain(self.repo), [
            "A  .minigitignore", "A  README.md", "A  src/app.py", "A  src/lib/util.py"])
        status = self.git("status").out
        self.assertIn("Changes to be committed:", status)
        self.assertIn("\tnew file:   README.md", status)

        res = self.git("commit", "-m", "initial import")
        self.assertRegex(res.out, rf"^\[main \(root-commit\) {HEX7}\] initial import\n"
                                  r" 4 files changed, 6 insertions\(\+\)\n( create mode 100644 \S+\n){4}$")
        first = self.head(self.repo)
        self.assertIn("nothing to commit, working tree clean", self.git("status").out)
        self.assertIn("nothing to commit", self.git_fail("commit", "-m", "again").out)

        # diff: working tree vs index, index vs HEAD, between commits
        self.put("README.md", "hello\nworld\n")
        self.assertEqual(self.porcelain(self.repo), [" M README.md"])
        diff = self.git("diff").out
        self.assertIn("diff --git a/README.md b/README.md\n", diff)
        self.assertIn("--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n hello\n+world\n", diff)
        self.assertEqual(self.git("diff", "--staged").out, "")
        self.git("add", "README.md")
        self.assertEqual(self.porcelain(self.repo), ["M  README.md"])
        self.assertEqual(self.git("diff").out, "")
        self.assertIn("@@ -1 +1,2 @@\n hello\n+world\n", self.git("diff", "--staged").out)
        res = self.git("commit", "-m", "add world", "-m", "A longer body.")
        self.assertRegex(res.out, rf"^\[main {HEX7}\] add world\n 1 file changed, 1 insertion\(\+\)\n$")
        second = self.head(self.repo)
        self.assertIn("+world\n", self.git("diff", "HEAD~1", "HEAD").out)
        self.assertIn("+world\n", self.git("diff", f"{first[:7]}..{second[:7]}").out)
        self.assertEqual(self.git("diff", "HEAD~1", "HEAD", "--", "src").out, "")
        self.put("src/app.py", "print('bye')\n")
        diff = self.git("diff", "HEAD~1").out  # a commit vs the working tree
        self.assertIn("+world\n", diff)
        self.assertIn("-print('hi')\n+print('bye')\n", diff)
        self.git("restore", "src/app.py")

        # log and show
        self.assertEqual(self.subjects(self.repo), ["add world", "initial import"])
        self.assertEqual(self.git("log", "--oneline").out.splitlines()[0],
                         f"{second[:7]} (HEAD -> main) add world")
        log = self.git("log", "-n", "1").out
        self.assertIn(f"commit {second} (HEAD -> main)\n", log)
        self.assertIn("Author: Test User <test@example.com>\n", log)
        self.assertIn("    add world\n", log)
        self.assertIn("    A longer body.\n", log)
        self.assertNotIn("initial import", log)
        show = self.git("show", "HEAD").out
        self.assertIn(f"commit {second}", show)
        self.assertIn("+world\n", show)
        self.assertEqual(self.git("show", "HEAD:src/lib/util.py").stdout, b"x = 1\r\ny = 2\r\n")
        self.assertEqual(self.get("src/lib/util.py"), b"x = 1\r\ny = 2\r\n")
        self.assertEqual(self.git("cat-file", "-t", "HEAD").out, "commit\n")

        # rm
        self.assertEqual(self.git("rm", "src/app.py").out, "rm 'src/app.py'\n")
        self.assertFalse(self.exists("src/app.py"))
        self.assertEqual(self.porcelain(self.repo), ["D  src/app.py"])
        self.git("commit", "-m", "remove app")
        self.assertEqual(self.subjects(self.repo), ["remove app", "add world", "initial import"])
        self.assertEqual(self.git("ls-files").out.splitlines(),
                         [".minigitignore", "README.md", "src/lib/util.py"])
        self.assertEqual(self.mg(self.repo, "fsck").out, "")

    def test_branches_checkout_and_merges(self):
        self.new_repo()
        base = "one\ntwo\nthree\nfour\nfive\n"
        c1 = self.commit_file("story.txt", base, "base")

        # branch: create, list, refuse a duplicate
        self.git("branch", "feature")
        self.assertEqual(self.git("branch").out, "  feature\n* main\n")
        self.assertIn("already exists", self.git_fail("branch", "feature", code=128).err)

        # checkout a branch and commit on it
        self.assertIn("Switched to branch 'feature'", self.git("checkout", "feature").err)
        self.assertEqual(self.git("branch").out, "* feature\n  main\n")
        self.put("story.txt", base.replace("one", "ONE"))
        self.put("feature.txt", "feature work\n")
        self.git("add", ".")
        self.git("commit", "-m", "feature work")
        feature_tip = self.head(self.repo)
        self.git("checkout", "main")
        self.assertEqual(self.get("story.txt"), base.encode())
        self.assertFalse(self.exists("feature.txt"))

        # checkout refuses to clobber uncommitted changes (and an untracked file in the way)
        self.put("story.txt", base + "local edit\n")
        res = self.git_fail("checkout", "feature")
        self.assertIn("Your local changes to the following files would be overwritten by checkout:", res.err)
        self.assertIn("\tstory.txt", res.err)
        self.assertIn("On branch main", self.git("status").out)
        self.assertEqual(self.get("story.txt"), (base + "local edit\n").encode())
        self.git("restore", "story.txt")
        self.put("feature.txt", "untracked, in the way\n")
        self.assertIn("feature.txt", self.git_fail("switch", "feature").err)
        os.remove(os.path.join(self.repo, "feature.txt"))
        self.assertEqual(self.porcelain(self.repo), [])

        # fast-forward merge
        res = self.git("merge", "feature")
        self.assertIn(f"Updating {c1[:7]}..{feature_tip[:7]}", res.out)
        self.assertIn("Fast-forward", res.out)
        self.assertEqual(self.head(self.repo), feature_tip)
        self.assertEqual(self.get("feature.txt"), b"feature work\n")
        self.assertIn("Already up to date.", self.git("merge", "feature").out)

        # true three-way merge: each side changes a different line
        self.git("switch", "-c", "topic")
        topic_tip = self.commit_file("story.txt", "ONE\ntwo\nthree\nfour\nFIVE\n", "topic edits five")
        self.git("switch", "main")
        main_tip = self.commit_file("story.txt", "ONE\ntwo\nTHREE\nfour\nfive\n", "main edits three")
        res = self.git("merge", "topic")
        self.assertIn("Merge made by the 'ort' strategy.", res.out)
        self.assertEqual(self.get("story.txt"), b"ONE\ntwo\nTHREE\nfour\nFIVE\n")
        self.assertEqual(self.rev(self.repo, "HEAD^1"), main_tip)
        self.assertEqual(self.rev(self.repo, "HEAD^2"), topic_tip)
        log = self.git("log", "-n", "1").out
        self.assertIn(f"Merge: {main_tip[:7]} {topic_tip[:7]}\n", log)
        self.assertIn("    Merge branch 'topic'\n", log)
        self.assertEqual(self.porcelain(self.repo), [])

        # conflicting merge: markers, status, refusals, abort
        self.git("switch", "-c", "clash")
        clash_tip = self.commit_file("story.txt", "ONE\ntwo (clash)\nTHREE\nfour\nFIVE\n", "clash edits two")
        self.git("switch", "main")
        main_tip = self.commit_file("story.txt", "ONE\ntwo (main)\nTHREE\nfour\nFIVE\n", "main edits two")
        self.put("notes.txt", "untracked and unrelated\n")  # must survive the abort
        res = self.git_fail("merge", "clash")
        self.assertIn("CONFLICT (content): Merge conflict in story.txt", res.out)
        self.assertIn("Automatic merge failed; fix conflicts and then commit the result.", res.out)
        self.assertEqual(self.get("story.txt"),
                         b"ONE\n<<<<<<< HEAD\ntwo (main)\n=======\ntwo (clash)\n>>>>>>> clash\nTHREE\nfour\nFIVE\n")
        self.assertEqual(self.porcelain(self.repo), ["?? notes.txt", "UU story.txt"])
        status = self.git("status").out
        self.assertIn("You have unmerged paths.", status)
        self.assertIn("both modified:   story.txt", status)
        self.assertIn("* Unmerged path story.txt", self.git("diff").out)
        self.assertIn("unresolved conflicts", self.git_fail("commit", "-m", "too early").err)
        self.assertIn("merge in progress", self.git_fail("switch", "feature").err)
        self.assertIn("MERGE_HEAD", self.git_fail("merge", "clash").err)
        self.assertEqual(self.git("merge", "--abort").returncode, 0)
        self.assertEqual(self.get("story.txt"), b"ONE\ntwo (main)\nTHREE\nfour\nFIVE\n")
        self.assertEqual(self.get("notes.txt"), b"untracked and unrelated\n")
        self.assertEqual(self.porcelain(self.repo), ["?? notes.txt"])
        self.assertEqual(self.head(self.repo), main_tip)
        self.assertIn("no merge to abort", self.git_fail("merge", "--abort").err)

        # the same conflict, resolved with add + commit (the prepared message is used)
        self.git_fail("merge", "clash")
        self.put("story.txt", "ONE\ntwo (main + clash)\nTHREE\nfour\nFIVE\n")
        self.git("add", "story.txt")
        self.assertIn("All conflicts fixed but you are still merging.", self.git("status").out)
        res = self.git("commit")
        self.assertRegex(res.out, rf"^\[main {HEX7}\] Merge branch 'clash'\n$")
        self.assertEqual(self.rev(self.repo, "HEAD^1"), main_tip)
        self.assertEqual(self.rev(self.repo, "HEAD^2"), clash_tip)
        self.assertEqual(self.porcelain(self.repo), ["?? notes.txt"])
        self.assertEqual(self.subjects(self.repo)[:3], ["Merge branch 'clash'", "main edits two", "clash edits two"])

        # branch deletion: merged is fine, unmerged needs -D, the current one is refused
        self.assertIn("Deleted branch clash", self.git("branch", "-d", "clash").out)
        self.git("switch", "-c", "experiment")
        self.commit_file("exp.txt", "exp\n", "experiment")
        self.git("switch", "main")
        self.assertFalse(self.exists("exp.txt"))
        self.assertIn("not fully merged", self.git_fail("branch", "-d", "experiment").err)
        self.git("branch", "-D", "experiment")
        self.assertIn("cannot delete the branch 'main'", self.git_fail("branch", "-d", "main").err)
        self.assertEqual(self.git("branch").out, "  feature\n* main\n  topic\n")

        # detached HEAD: commit there, then come back
        merged = self.head(self.repo)
        self.assertIn(f"HEAD is now at {main_tip[:7]}", self.git("checkout", "HEAD~1").err)
        self.assertIn(f"HEAD detached at {main_tip[:7]}", self.git("status").out)
        res = self.git("commit", "--allow-empty", "-m", "detached work")
        self.assertRegex(res.out, rf"^\[detached HEAD {HEX7}\] detached work\n$")
        self.assertIn("Switched to branch 'main'", self.git("switch", "main").err)
        self.assertEqual(self.head(self.repo), merged)

    def test_reset_and_restore(self):
        self.new_repo()
        c1 = self.commit_file("file.txt", "v1\n", "c1")
        c2 = self.commit_file("file.txt", "v2\n", "c2")
        self.put("file.txt", "v3\n")
        self.put("extra.txt", "extra\n")
        self.git("add", ".")
        self.git("commit", "-m", "c3")
        c3 = self.head(self.repo)
        self.put("untracked.txt", "keep me\n")

        # --soft: only HEAD moves
        self.assertEqual(self.git("reset", "--soft", "HEAD~1").out, "")
        self.assertEqual(self.head(self.repo), c2)
        self.assertEqual(self.rev(self.repo, "ORIG_HEAD"), c3)
        self.assertEqual(self.porcelain(self.repo), ["A  extra.txt", "M  file.txt", "?? untracked.txt"])
        self.assertEqual(self.get("file.txt"), b"v3\n")
        self.git("reset", "--hard", "ORIG_HEAD")
        self.assertEqual(self.head(self.repo), c3)
        self.assertEqual(self.porcelain(self.repo), ["?? untracked.txt"])

        # --mixed (the default): HEAD and the index move, the files stay
        self.git("reset", "HEAD~1")
        self.assertEqual(self.head(self.repo), c2)
        self.assertEqual(self.porcelain(self.repo), ["?? extra.txt", " M file.txt", "?? untracked.txt"])
        self.assertEqual(self.get("file.txt"), b"v3\n")
        self.git("reset", "--hard", c3)

        # --hard: HEAD, the index and the working tree move; untracked files survive
        res = self.git("reset", "--hard", "HEAD~2")
        self.assertEqual(res.out, f"HEAD is now at {c1[:7]} c1\n")
        self.assertEqual(self.get("file.txt"), b"v1\n")
        self.assertFalse(self.exists("extra.txt"))
        self.assertEqual(self.get("untracked.txt"), b"keep me\n")
        self.assertEqual(self.porcelain(self.repo), ["?? untracked.txt"])
        self.assertEqual(self.subjects(self.repo), ["c1"])
        self.git("reset", "--hard", c3)  # the commits are still there
        self.assertEqual(self.subjects(self.repo), ["c3", "c2", "c1"])
        self.assertEqual(self.get("extra.txt"), b"extra\n")

        # restore: working tree from the index, index from HEAD, file from another commit
        self.put("file.txt", "scribble\n")
        self.git("restore", "file.txt")
        self.assertEqual(self.get("file.txt"), b"v3\n")
        self.put("file.txt", "staged change\n")
        self.git("add", "file.txt")
        self.assertEqual(self.porcelain(self.repo), ["M  file.txt", "?? untracked.txt"])
        self.git("restore", "--staged", "file.txt")
        self.assertEqual(self.porcelain(self.repo), [" M file.txt", "?? untracked.txt"])
        self.assertEqual(self.get("file.txt"), b"staged change\n")
        self.git("checkout", "--", "file.txt")
        self.assertEqual(self.get("file.txt"), b"v3\n")
        self.git("restore", "--source", c1, "file.txt")
        self.assertEqual(self.get("file.txt"), b"v1\n")
        self.assertEqual(self.porcelain(self.repo), [" M file.txt", "?? untracked.txt"])
        self.git("checkout", c2, "--", "file.txt")  # index and working tree from c2
        self.assertEqual(self.get("file.txt"), b"v2\n")
        self.assertEqual(self.porcelain(self.repo), ["M  file.txt", "?? untracked.txt"])
        self.git("reset", "--hard")
        self.assertEqual(self.porcelain(self.repo), ["?? untracked.txt"])

        # reset -- <path> unstages without touching the file
        self.put("extra.txt", "changed\n")
        self.git("add", "extra.txt")
        self.git("reset", "--", "extra.txt")
        self.assertEqual(self.porcelain(self.repo), [" M extra.txt", "?? untracked.txt"])
        self.assertEqual(self.get("extra.txt"), b"changed\n")
        self.assertEqual(self.head(self.repo), c3)


# ---------------------------------------------------------------------------
# Several users through a live server
# ---------------------------------------------------------------------------

class MultiUserWorkflowTests(CliCase):
    server: ServerProcess
    tokens: dict[str, str]

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(tmp.cleanup)
        cls.root = os.path.realpath(tmp.name)
        cls.admin_env = make_env(os.path.join(cls.root, "admin"))
        cls.srv_root = os.path.join(cls.root, "server")
        run(["server", "init", cls.srv_root], cwd=cls.root, env=cls.admin_env)
        cls.tokens = {}
        for user in ("alice", "bob"):
            res = run(["server", "add-user", cls.srv_root, user], cwd=cls.root, env=cls.admin_env)
            cls.tokens[user] = res.out.strip()
            assert cls.tokens[user] and "\n" not in cls.tokens[user], res
        cls.server = ServerProcess(cls.srv_root, env=cls.admin_env)
        cls.addClassCleanup(cls.server.stop)  # cleanups run LIFO: the server stops before tmp goes
        cls.server.start()

    def setUp(self):
        self.env = self.admin_env
        self.work = os.path.join(self.root, self._testMethodName)
        os.makedirs(self.work)

    # -- helpers ----------------------------------------------------------
    def user_env(self, user, token=None):
        """Environment for ``user`` (credentials via MINIGIT_USER/MINIGIT_TOKEN)."""
        home = os.path.join(self.work, "home-" + user + ("-bad" if token else ""))
        return make_env(home, user=user, token=token or self.tokens[user])

    def create_repo(self, name, *flags) -> str:
        res = run(["server", "create-repo", self.srv_root, name, *flags], cwd=self.root, env=self.admin_env)
        self.assertEqual(res.out.strip(), "/" + name)
        return f"{self.server.url}/{name}"

    def server_repo(self, name) -> str:
        return os.path.join(self.srv_root, "repos", name)

    def server_head(self, name) -> str:
        return self.head(self.server_repo(name))

    def assert_fsck_clean(self, name):
        self.assertEqual(self.mg(self.server_repo(name), "fsck").out, "")

    def clone(self, url, dest, env):
        res = self.mg(self.work, "clone", url, dest, env=env)
        return os.path.join(self.work, dest), res

    def commit(self, wt, env, rel, data, msg) -> str:
        write(os.path.join(wt, rel), data)
        self.mg(wt, "add", rel, env=env)
        self.mg(wt, "commit", "-m", msg, env=env)
        return self.head(wt, env)

    def assert_rejected(self, res, url, branch="main", pull_hint=True):
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn(f"To {url}\n", res.err)
        self.assertIn(f" ! [rejected]        {branch} -> {branch} (", res.err)
        self.assertIn(f"error: failed to push some refs to '{url}'", res.err)
        if pull_hint:
            self.assertIn("'minigit pull'", res.err)
        else:  # e.g. a forced push the repository refuses: pulling would not help
            self.assertNotIn("minigit pull", res.err)

    # -- tests ------------------------------------------------------------
    def test_two_users_push_rejected_then_pull_merge_push(self):
        url = self.create_repo("collab")
        a_env, b_env = self.user_env("alice"), self.user_env("bob")

        alice, res = self.clone(url, "alice", a_env)
        self.assertIn("Cloning into 'alice'...", res.out)
        self.assertIn("warning: You appear to have cloned an empty repository.", res.err)
        self.assertEqual(self.mg(alice, "remote", "-v", env=a_env).out,
                         f"origin\t{url} (fetch)\norigin\t{url} (push)\n")
        root = self.commit(alice, a_env, "README.md", "# collab\n", "initial")
        res = self.mg(alice, "push", "-u", "origin", "main", env=a_env)
        self.assertIn(f"To {url}\n", res.out)
        self.assertIn(" * [new branch]      main -> main\n", res.out)
        self.assertIn("branch 'main' set up to track 'origin/main'.", res.out)
        self.assertEqual(self.server_head("collab"), root)

        bob, res = self.clone(url, "bob", b_env)
        self.assertNotIn("warning", res.err)
        self.assertEqual(read(os.path.join(bob, "README.md")), b"# collab\n")
        self.assertEqual(self.head(bob, b_env), root)
        self.assertEqual(self.mg(bob, "branch", "-r", env=b_env).out, "  origin/main\n")
        self.assertIn("Your branch is up to date with 'origin/main'.", self.mg(bob, "status", env=b_env).out)

        # Both commit; alice pushes first.
        alice_tip = self.commit(alice, a_env, "alice.txt", "from alice\n", "alice's change")
        bob_tip = self.commit(bob, b_env, "bob.txt", "from bob\n", "bob's change")
        res = self.mg(alice, "push", env=a_env)
        self.assertRegex(res.out, rf"{root[:7]}\.\.{alice_tip[:7]}\s+main -> main")
        self.assertEqual(self.server_head("collab"), alice_tip)

        # Bob's push is rejected: the remote has a commit he does not have.
        self.assert_rejected(self.mg_fail(bob, "push", env=b_env), url)
        self.assertEqual(self.server_head("collab"), alice_tip)
        # After a fetch he has that commit, but his branch still does not contain it.
        res = self.mg(bob, "fetch", env=b_env)
        self.assertIn(f"From {url}\n", res.out)
        self.assertRegex(res.out, rf"{root[:7]}\.\.{alice_tip[:7]}\s+main -> origin/main")
        self.assertIn("have diverged", self.mg(bob, "status", env=b_env).out)
        res = self.mg_fail(bob, "push", env=b_env)
        self.assert_rejected(res, url)
        self.assertIn("main -> main (non-fast-forward)", res.err)
        # The server enforces it as well: forced updates are disabled for this repository.
        res = self.mg_fail(bob, "push", "--force", env=b_env)
        self.assert_rejected(res, url, pull_hint=False)
        self.assertIn("(forced update disabled)", res.err)
        self.assertIn("hint: this repository does not allow forced pushes", res.err)
        self.assertEqual(self.server_head("collab"), alice_tip)

        # Bob pulls (a real merge commit) and pushes.
        res = self.mg(bob, "pull", env=b_env)
        self.assertIn("Merge made by the 'ort' strategy.", res.out)
        self.assertEqual(read(os.path.join(bob, "alice.txt")), b"from alice\n")
        self.assertEqual(read(os.path.join(bob, "bob.txt")), b"from bob\n")
        merge_tip = self.head(bob, b_env)
        self.assertEqual(self.rev(bob, "HEAD^1", b_env), bob_tip)
        self.assertEqual(self.rev(bob, "HEAD^2", b_env), alice_tip)
        self.assertIn(f"    Merge branch 'main' of {url}\n", self.mg(bob, "log", "-n", "1", env=b_env).out)
        self.assertIn("ahead of 'origin/main' by 2 commits", self.mg(bob, "status", env=b_env).out)
        res = self.mg(bob, "push", env=b_env)
        self.assertRegex(res.out, rf"{alice_tip[:7]}\.\.{merge_tip[:7]}\s+main -> main")
        self.assertEqual(self.server_head("collab"), merge_tip)

        # Alice fast-forwards; everybody now has the same history.
        res = self.mg(alice, "pull", env=a_env)
        self.assertIn(f"{alice_tip[:7]}..{merge_tip[:7]}", res.out)
        self.assertIn("Fast-forward", res.out)
        self.assertEqual(read(os.path.join(alice, "bob.txt")), b"from bob\n")
        self.assertEqual(self.head(alice, a_env), merge_tip)
        self.assertEqual(self.mg(alice, "fetch", env=a_env).out, "")  # nothing left to transfer
        self.assertIn("Already up to date.", self.mg(alice, "pull", env=a_env).out)
        self.assertEqual(self.mg(alice, "push", env=a_env).out, "Everything up-to-date\n")
        self.assertEqual(self.subjects(alice, a_env), self.subjects(bob, b_env))
        self.assertEqual(self.mg(alice, "log", "--oneline", "-n", "1", env=a_env).out,
                         f"{merge_tip[:7]} (HEAD -> main, origin/main) Merge branch 'main' of {url}\n")
        self.assert_fsck_clean("collab")

    def test_conflicting_edit_is_resolved_through_pull(self):
        url = self.create_repo("conflict")
        a_env, b_env = self.user_env("alice"), self.user_env("bob")
        alice, _ = self.clone(url, "alice", a_env)
        self.commit(alice, a_env, "plan.txt", "goal: ship\nowner: tbd\ndate: soon\n", "plan")
        self.mg(alice, "push", "-u", "origin", "main", env=a_env)
        bob, _ = self.clone(url, "bob", b_env)

        alice_tip = self.commit(alice, a_env, "plan.txt", "goal: ship\nowner: alice\ndate: soon\n", "alice owns it")
        self.mg(alice, "push", env=a_env)
        bob_tip = self.commit(bob, b_env, "plan.txt", "goal: ship\nowner: bob\ndate: soon\n", "bob owns it")
        self.assert_rejected(self.mg_fail(bob, "push", env=b_env), url)

        res = self.mg_fail(bob, "pull", env=b_env)
        self.assertIn("CONFLICT (content): Merge conflict in plan.txt", res.out)
        self.assertIn("Automatic merge failed; fix conflicts and then commit the result.", res.out)
        self.assertEqual(read(os.path.join(bob, "plan.txt")),
                         b"goal: ship\n<<<<<<< HEAD\nowner: bob\n=======\nowner: alice\n"
                         b">>>>>>> origin/main\ndate: soon\n")
        self.assertEqual(self.porcelain(bob, b_env), ["UU plan.txt"])
        self.assertIn("unresolved conflicts", self.mg_fail(bob, "commit", "-m", "not yet", env=b_env).err)

        write(os.path.join(bob, "plan.txt"), "goal: ship\nowner: alice and bob\ndate: soon\n")
        self.mg(bob, "add", "plan.txt", env=b_env)
        res = self.mg(bob, "commit", env=b_env)
        self.assertRegex(res.out, rf"^\[main {HEX7}\] Merge branch 'main' of {re.escape(url)}\n$")
        resolved = self.head(bob, b_env)
        self.assertEqual(self.rev(bob, "HEAD^1", b_env), bob_tip)
        self.assertEqual(self.rev(bob, "HEAD^2", b_env), alice_tip)
        self.mg(bob, "push", env=b_env)
        self.assertEqual(self.server_head("conflict"), resolved)

        res = self.mg(alice, "pull", env=a_env)
        self.assertIn("Fast-forward", res.out)
        self.assertEqual(read(os.path.join(alice, "plan.txt")), b"goal: ship\nowner: alice and bob\ndate: soon\n")
        self.assertEqual(self.head(alice, a_env), resolved)
        self.assertEqual(self.porcelain(alice, a_env), [])
        self.assert_fsck_clean("conflict")

    def test_second_repository_clone_and_write_permissions(self):
        app_url = self.create_repo("app")
        docs_url = self.create_repo("docs", "--writers", "alice")  # everybody reads, only alice writes
        a_env, b_env = self.user_env("alice"), self.user_env("bob")

        app_a, _ = self.clone(app_url, "app-alice", a_env)
        self.commit(app_a, a_env, "main.py", "print('app')\n", "app code")
        self.mg(app_a, "push", "-u", "origin", "main", env=a_env)
        docs_a, _ = self.clone(docs_url, "docs-alice", a_env)
        docs_tip = self.commit(docs_a, a_env, "guide.md", "# Guide\n", "docs")
        self.mg(docs_a, "push", "-u", "origin", "main", env=a_env)

        # Bob clones both repositories into their default directory names.
        bob_dir = os.path.join(self.work, "bob")
        os.makedirs(bob_dir)
        res = self.mg(bob_dir, "clone", docs_url, env=b_env)
        self.assertIn("Cloning into 'docs'...", res.out)
        res = self.mg(bob_dir, "clone", app_url, env=b_env)
        self.assertIn("Cloning into 'app'...", res.out)
        docs_b, app_b = os.path.join(bob_dir, "docs"), os.path.join(bob_dir, "app")
        self.assertEqual(sorted(os.listdir(docs_b)), [".minigit", "guide.md"])
        self.assertEqual(sorted(os.listdir(app_b)), [".minigit", "main.py"])
        self.assertEqual(read(os.path.join(docs_b, "guide.md")), b"# Guide\n")
        self.assertEqual(self.head(docs_b, b_env), docs_tip)
        self.assertEqual(self.mg(docs_b, "remote", "get-url", "origin", env=b_env).out.strip(), docs_url)

        # Bob may read docs but not write to it; the app repository takes his push.
        self.commit(docs_b, b_env, "typo.md", "fix\n", "bob's docs fix")
        res = self.mg_fail(docs_b, "push", env=b_env)
        self.assertIn("permission denied", res.err)
        self.assertEqual(self.server_head("docs"), docs_tip)
        bob_app = self.commit(app_b, b_env, "feature.py", "pass\n", "bob's feature")
        self.mg(app_b, "push", env=b_env)
        self.assertEqual(self.server_head("app"), bob_app)
        self.mg(app_a, "pull", env=a_env)
        self.assertEqual(read(os.path.join(app_a, "feature.py")), b"pass\n")
        self.assertFalse(os.path.exists(os.path.join(app_a, "guide.md")))
        self.assert_fsck_clean("app")
        self.assert_fsck_clean("docs")

    def test_bad_token_is_rejected(self):
        url = self.create_repo("secure")
        a_env = self.user_env("alice")
        alice, _ = self.clone(url, "alice", a_env)
        tip = self.commit(alice, a_env, "secret.txt", "s3cret\n", "secret")
        self.mg(alice, "push", "-u", "origin", "main", env=a_env)

        bad_env = self.user_env("alice", token="not-the-right-token")
        res = self.mg_fail(self.work, "clone", url, "intruder", env=bad_env)
        self.assertIn(f"authentication failed for '{url}'", res.err)
        self.assertFalse(os.path.exists(os.path.join(self.work, "intruder")))  # nothing left behind

        # A wrong token in the URL, an unknown user and no credentials at all fail the same way.
        anon_env = make_env(os.path.join(self.work, "home-anon"))
        hostport = self.server.url[len("http://"):]
        for label, clone_url, env in (
                ("url-token", f"http://alice:wrong@{hostport}/secure", anon_env),
                ("unknown-user", url, make_env(os.path.join(self.work, "home-mallory"),
                                               user="mallory", token=self.tokens["alice"])),
                ("anonymous", url, anon_env)):
            with self.subTest(label):
                res = self.mg_fail(self.work, "clone", clone_url, label, env=env)
                self.assertIn("authentication failed" if label != "anonymous" else "authentication required",
                              res.err)
                self.assertFalse(os.path.exists(os.path.join(self.work, label)))

        # In an existing clone, a bad token can neither push nor fetch.
        self.commit(alice, a_env, "more.txt", "more\n", "more")
        self.assertIn("authentication failed", self.mg_fail(alice, "push", env=bad_env).err)
        self.assertIn("authentication failed", self.mg_fail(alice, "fetch", env=bad_env).err)
        self.assertIn("authentication failed", self.mg_fail(alice, "pull", env=bad_env).err)
        self.assertEqual(self.server_head("secure"), tip)
        self.mg(alice, "push", env=a_env)  # the right token still works
        self.assertEqual(self.server_head("secure"), self.head(alice, a_env))

    def test_concurrent_pushes_have_exactly_one_winner(self):
        url = self.create_repo("race")
        a_env = self.user_env("alice")
        seed, _ = self.clone(url, "seed", a_env)
        self.commit(seed, a_env, "base.txt", "base\n", "base")
        self.mg(seed, "push", "-u", "origin", "main", env=a_env)

        racers = []
        for i in range(4):
            env = self.user_env(("alice", "bob")[i % 2])
            wt, _ = self.clone(url, f"racer{i}", env)
            self.commit(wt, env, f"racer{i}.txt", f"{i}\n", f"racer {i}")
            racers.append((wt, env))

        results: list = [None] * len(racers)
        barrier = threading.Barrier(len(racers))

        def push(i):
            wt, env = racers[i]
            barrier.wait()
            results[i] = run(["push"], cwd=wt, env=env, check=False)

        threads = [threading.Thread(target=push, args=(i,)) for i in range(len(racers))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertTrue(all(r is not None for r in results), results)
        codes = [r.returncode for r in results]
        self.assertEqual(codes.count(0), 1, "\n\n".join(str(r) for r in results))
        winner = codes.index(0)
        for i, res in enumerate(results):
            if i != winner:
                self.assert_rejected(res, url)
        self.assertEqual(self.server_head("race"), self.head(*racers[winner]))
        self.assert_fsck_clean("race")

        # Every loser recovers with pull + push, one after another.
        for i, (wt, env) in enumerate(racers):
            if i != winner:
                self.mg(wt, "pull", env=env)
                self.mg(wt, "push", env=env)
        self.mg(seed, "pull", env=a_env)
        for i in range(len(racers)):
            self.assertEqual(read(os.path.join(seed, f"racer{i}.txt")), f"{i}\n".encode())
        self.assertEqual(self.server_head("race"), self.head(seed, a_env))
        self.assert_fsck_clean("race")


# ---------------------------------------------------------------------------
# Entry points, help and exit codes
# ---------------------------------------------------------------------------

class EntryPointTests(CliCase):
    def setUp(self):
        self.tmp = self.make_tmp()
        self.env = make_env(self.tmp)

    def test_help_version_and_exit_codes(self):
        self.assertEqual(self.mg(self.tmp, "--version").out, "minigit 0.4.0\n")
        res = self.mg(self.tmp, "--help")
        for command in ("init", "add", "commit", "merge", "clone", "push", "pull", "serve"):
            self.assertIn(command, res.out)
        self.assertIn("--message", self.mg(self.tmp, "commit", "--help").out)
        self.assertIn("usage:", self.mg(self.tmp, "help", "merge").out)
        self.assertIn("is not a minigit command", self.mg_fail(self.tmp, "frobnicate", code=1).err)   # git's exit code
        self.mg_fail(self.tmp, code=1)  # no command at all: the help, exit 1
        self.mg_fail(self.tmp, "commit", "--no-such-flag", code=129)
        res = self.mg_fail(self.tmp, "status", code=128)  # not inside a repository
        self.assertTrue(res.err.startswith("fatal: not a minigit repository"), res.err)

    def _env_with_this_python(self) -> dict:
        """PATH where ``python``/``python3`` are the interpreter running the tests."""
        env = dict(self.env)
        if os.name == "nt":
            shim = os.path.dirname(sys.executable)
        else:
            shim = os.path.join(self.tmp, "shim")
            os.makedirs(shim)
            for name in ("python", "python3"):
                os.symlink(sys.executable, os.path.join(shim, name))
        env["PATH"] = shim + os.pathsep + env.get("PATH", "")
        env.pop("PYTHONPATH", None)  # the entry script finds the package by itself
        return env

    def test_entry_scripts(self):
        script = os.path.join(ROOT, "bin", "minigit")
        env = self._env_with_this_python()
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)

        def via_script(*args):
            if os.name == "nt":
                # /s /c ""<script>" <args>"" keeps the quoting intact even with spaces in paths.
                bat = os.path.join(ROOT, "bin", "minigit.cmd")
                cmd = f'cmd /d /s /c ""{bat}" {subprocess.list2cmdline(args)}"'
            else:
                cmd = ["sh", script, *args]
            return subprocess.run(cmd, cwd=work, env=env, capture_output=True, timeout=60)

        proc = subprocess.run([sys.executable, script, "--version"], cwd=work, env=env,
                              capture_output=True, timeout=60)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, b"minigit 0.4.0"), proc)
        if os.name == "nt" and not shutil.which("cmd"):  # pragma: no cover
            self.skipTest("cmd.exe is not available")
        proc = via_script("--version")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, b"minigit 0.4.0"), proc)

        self.assertEqual(via_script("init").returncode, 0)
        write(os.path.join(work, "a.txt"), "via the entry script\n")
        self.assertEqual(via_script("add", "a.txt").returncode, 0)
        proc = via_script("commit", "-m", "made by the entry script")
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(self.subjects(work), ["made by the entry script"])
        self.assertEqual(via_script("merge").returncode, 129)             # usage error
        self.assertEqual(via_script("switch", "nowhere").returncode, 128) # fatal: invalid reference


if __name__ == "__main__":
    unittest.main()

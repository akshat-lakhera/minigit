"""Acceptance tests for ``minigit stash`` (design: ``features/stash.md``).

Every test drives the real command line as a subprocess (``tests.helpers.run``)
in a temporary directory. Run them from the project root with::

    python -m unittest tests.test_stash -v
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest

from tests.helpers import ServerProcess, make_env, read, run, write

HEX40 = re.compile(r"^[0-9a-f]{40}$")
BASE_A = b"one\ntwo\nthree\n"
BASE_B = b"bee\n"


class _StashCase(unittest.TestCase):
    """A repository ``wt`` with one commit ("base": a.txt, b.txt) and CLI helpers."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.env = make_env(self.tmp)
        self.wt = os.path.join(self.tmp, "wt")
        self.mg(self.tmp, "init", "wt")
        self.put("a.txt", BASE_A)
        self.put("b.txt", BASE_B)
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.head()
        self.wip = f"WIP on main: {self.base[:7]} base"

    # -- running the CLI ------------------------------------------------------
    def mg(self, cwd, *args, env=None):
        return run(args, cwd=cwd, env=env or self.env)

    def mg_fail(self, cwd, *args, code=1, env=None):
        res = run(args, cwd=cwd, env=env or self.env, check=False)
        self.assertEqual(res.returncode, code, str(res))
        return res

    def git(self, *args, env=None):
        return self.mg(self.wt, *args, env=env)

    def git_fail(self, *args, code=1, env=None):
        return self.mg_fail(self.wt, *args, code=code, env=env)

    # -- files ----------------------------------------------------------------
    def path(self, rel):
        return os.path.join(self.wt, *rel.split("/"))

    def put(self, rel, data):
        write(self.path(rel), data)

    def get(self, rel):
        return read(self.path(rel))

    def exists(self, rel):
        return os.path.lexists(self.path(rel))

    # -- state ----------------------------------------------------------------
    def head(self):
        sha = self.git("rev-parse", "HEAD").out.strip()
        self.assertRegex(sha, HEX40)
        return sha

    def porcelain(self):
        return self.git("status", "--porcelain").out.splitlines()

    def stash_list(self):
        return self.git("stash", "list").out.splitlines()

    def stash_file(self):
        return self.path(".minigit/stash")

    def stash_shas(self):
        """The on-disk stack: one 40-hex sha per line, newest first."""
        if not os.path.exists(self.stash_file()):
            return []
        data = read(self.stash_file())
        self.assertTrue(data.endswith(b"\n"), data)
        shas = data.decode("ascii").splitlines()
        for sha in shas:
            self.assertRegex(sha, HEX40)
        return shas

    def commit_lines(self, sha):
        return self.git("cat-file", "-p", sha).out.split("\n")

    def parents_of(self, sha):
        return [line[len("parent "):] for line in self.commit_lines(sha) if line.startswith("parent ")]

    def tree_of(self, sha):
        return self.commit_lines(sha)[0][len("tree "):]

    def message_of(self, sha):
        lines = self.commit_lines(sha)
        return "\n".join(lines[lines.index("") + 1:]).rstrip("\n")


class StashPushTests(_StashCase):
    def test_push_saves_tracked_changes_and_cleans_the_worktree(self):
        self.put("a.txt", b"one\nTWO\nthree\n")
        self.put("c.txt", b"new file\n")
        self.git("add", "c.txt")
        self.put("u.txt", b"untracked\n")
        res = self.git("stash")  # no subcommand means push
        self.assertEqual(res.out, f"Saved working directory and index state {self.wip}\n")
        self.assertEqual(self.get("a.txt"), BASE_A)
        self.assertFalse(self.exists("c.txt"))
        self.assertEqual(self.get("u.txt"), b"untracked\n")  # untracked files are left alone
        self.assertEqual(self.porcelain(), ["?? u.txt"])
        self.assertEqual(self.head(), self.base)              # HEAD never moves
        self.assertEqual(self.stash_list(), [f"stash@{{0}}: {self.wip}"])

    def test_nothing_to_save(self):
        res = self.git("stash")
        self.assertEqual(res.out, "No local changes to save\n")
        self.assertEqual(self.stash_list(), [])
        self.assertFalse(os.path.exists(self.stash_file()))
        self.put("u.txt", b"untracked only\n")  # untracked files need -u
        self.assertEqual(self.git("stash", "push").out, "No local changes to save\n")
        self.assertEqual(self.get("u.txt"), b"untracked only\n")
        self.put(".minigitignore", b"*.log\n")
        self.git("add", ".minigitignore")
        self.git("commit", "-m", "ignore logs")
        os.remove(self.path("u.txt"))
        self.put("debug.log", b"ignored\n")  # ignored files are never stashed, even with -u
        self.assertEqual(self.git("stash", "-u").out, "No local changes to save\n")
        self.assertEqual(self.get("debug.log"), b"ignored\n")
        self.assertEqual(self.stash_list(), [])

    def test_message_and_newest_first_listing(self):
        self.put("a.txt", b"first\n")
        self.git("stash", "push", "-m", "first try")
        self.put("b.txt", b"second\n")
        self.git("stash")
        self.put("a.txt", b"third\n")
        self.git("stash", "--message", "third try")  # options without a subcommand imply push
        self.assertEqual(self.stash_list(), [
            "stash@{0}: On main: third try",
            f"stash@{{1}}: {self.wip}",
            "stash@{2}: On main: first try",
        ])
        listed = self.mg(self.tmp, "-C", "wt", "stash", "list").out  # -C works like for any command
        self.assertEqual(listed.splitlines(), self.stash_list())

    def test_detached_head_label(self):
        self.git("switch", "--detach", "HEAD")
        self.put("a.txt", b"detached work\n")
        res = self.git("stash")
        label = f"WIP on (no branch): {self.base[:7]} base"
        self.assertEqual(res.out, f"Saved working directory and index state {label}\n")
        self.assertEqual(self.stash_list(), [f"stash@{{0}}: {label}"])
        self.git("stash", "pop")
        self.assertEqual(self.get("a.txt"), b"detached work\n")

    def test_unborn_head_is_refused(self):
        self.mg(self.tmp, "init", "fresh")
        fresh = os.path.join(self.tmp, "fresh")
        write(os.path.join(fresh, "x.txt"), b"x\n")
        self.mg(fresh, "add", "x.txt")
        res = self.mg_fail(fresh, "stash")
        self.assertIn("initial commit", res.err)
        self.assertEqual(self.mg(fresh, "status", "--porcelain").out, "A  x.txt\n")
        self.assertFalse(os.path.exists(os.path.join(fresh, ".minigit", "stash")))

    def test_missing_identity_changes_nothing(self):
        env = dict(self.env)
        env.pop("MINIGIT_AUTHOR_NAME")
        env.pop("MINIGIT_AUTHOR_EMAIL")
        self.put("a.txt", b"changed\n")
        res = self.git_fail("stash", env=env)
        self.assertIn("please set your identity", res.err)
        self.assertEqual(self.get("a.txt"), b"changed\n")
        self.assertEqual(self.porcelain(), [" M a.txt"])
        self.assertEqual(self.stash_list(), [])

    def test_refused_while_a_merge_is_in_progress(self):
        self.git("branch", "side")
        self.put("a.txt", b"one\nmain\nthree\n")
        self.git("commit", "-a", "-m", "main edit")
        self.git("switch", "side")
        self.put("a.txt", b"one\nside\nthree\n")
        self.git("commit", "-a", "-m", "side edit")
        self.git("switch", "main")
        self.put("b.txt", b"stashed before the merge\n")
        self.git("stash")
        self.git_fail("merge", "side")  # conflict: MERGE_HEAD + unmerged a.txt
        marked = self.get("a.txt")
        self.assertIn("merge in progress", self.git_fail("stash").err)
        self.assertIn("merge in progress", self.git_fail("stash", "pop").err)
        self.assertEqual(self.get("a.txt"), marked)
        self.assertEqual(self.get("b.txt"), BASE_B)
        self.assertEqual(len(self.stash_list()), 1)
        self.git("merge", "--abort")
        self.git("stash", "pop")
        self.assertEqual(self.get("b.txt"), b"stashed before the merge\n")

    def test_untracked_directory_in_the_way_needs_include_untracked(self):
        self.put("dd", b"dd file\n")
        self.git("add", "dd")
        self.git("commit", "-m", "add dd")
        os.remove(self.path("dd"))
        self.put("dd/x.txt", b"untracked in a dir\n")  # a directory where HEAD has a file
        self.assertEqual(self.porcelain(), [" D dd", "?? dd/x.txt"])
        res = self.git_fail("stash")  # restoring dd from HEAD would destroy dd/x.txt
        self.assertIn("would be overwritten", res.err)
        self.assertIn("dd", res.err)
        self.assertEqual(self.get("dd/x.txt"), b"untracked in a dir\n")
        self.assertEqual(self.stash_list(), [])
        self.git("stash", "-u")  # with -u the untracked file is saved first
        self.assertEqual(self.get("dd"), b"dd file\n")
        self.assertEqual(self.porcelain(), [])
        self.git("stash", "pop")
        self.assertEqual(self.get("dd/x.txt"), b"untracked in a dir\n")
        self.assertEqual(self.porcelain(), [" D dd", "?? dd/x.txt"])

    def test_staged_deletion_with_the_file_still_on_disk_keeps_its_content(self):
        self.git("rm", "--cached", "b.txt")
        self.put("b.txt", b"edited after rm --cached\n")
        self.assertEqual(self.porcelain(), ["?? b.txt", "D  b.txt"])  # sorted by path, then XY
        self.git("stash")
        self.assertEqual(self.get("b.txt"), BASE_B)
        self.assertEqual(self.porcelain(), [])
        self.git("stash", "pop")
        # The content is never lost; like git, the staged deletion itself is not restored.
        self.assertEqual(self.get("b.txt"), b"edited after rm --cached\n")
        self.assertEqual(self.porcelain(), [" M b.txt"])

    def test_bare_repository_is_refused(self):
        self.mg(self.tmp, "init", "--bare", "bare.git")
        res = self.mg_fail(os.path.join(self.tmp, "bare.git"), "stash", "list")
        self.assertIn("work tree", res.err)


class StashApplyPopDropTests(_StashCase):
    def test_pop_restores_changes_and_drops_the_entry(self):
        self.put("a.txt", b"one\nTWO\nthree\n")
        os.remove(self.path("b.txt"))
        self.put("c.txt", b"new file\n")
        self.put("sub/deep/d.txt", b"nested new file\n")
        self.git("add", "c.txt", "sub")
        self.git("stash", "push")
        [sha] = self.stash_shas()
        self.assertEqual(self.porcelain(), [])
        self.assertEqual(self.get("b.txt"), BASE_B)
        self.assertFalse(self.exists("sub"))  # emptied directories are pruned
        res = self.git("stash", "pop")
        self.assertIn(f"Dropped stash@{{0}} ({sha})\n", res.out)
        self.assertEqual(self.get("a.txt"), b"one\nTWO\nthree\n")
        self.assertFalse(self.exists("b.txt"))
        self.assertEqual(self.get("c.txt"), b"new file\n")
        self.assertEqual(self.get("sub/deep/d.txt"), b"nested new file\n")
        # Without --index (not supported) only new files are re-staged, as in git.
        self.assertEqual(self.porcelain(), [" M a.txt", " D b.txt", "A  c.txt", "A  sub/deep/d.txt"])
        self.assertEqual(self.stash_list(), [])
        self.assertFalse(os.path.exists(self.stash_file()))
        self.assertEqual(self.head(), self.base)

    def test_staged_changes_come_back_unstaged_with_worktree_content(self):
        self.put("a.txt", b"staged\n")
        self.git("add", "a.txt")
        self.put("a.txt", b"staged then edited\n")
        self.assertEqual(self.porcelain(), ["MM a.txt"])
        self.git("stash")
        self.assertEqual(self.porcelain(), [])
        self.git("stash", "pop")
        self.assertEqual(self.get("a.txt"), b"staged then edited\n")
        self.assertEqual(self.porcelain(), [" M a.txt"])

    def test_apply_keeps_the_entry_and_references_select_entries(self):
        self.put("a.txt", b"a change\n")
        self.git("stash", "-m", "a-change")
        self.put("b.txt", b"b change\n")
        self.git("stash", "-m", "b-change")
        self.assertEqual(self.git("stash", "apply").out, "")  # newest entry, nothing printed
        self.assertEqual(self.get("b.txt"), b"b change\n")
        self.assertEqual(self.get("a.txt"), BASE_A)
        self.assertEqual(len(self.stash_list()), 2)
        self.git("reset", "--hard")
        res = self.git("stash", "pop", "1")  # a bare number is accepted (handy in PowerShell)
        self.assertIn("Dropped stash@{1} (", res.out)
        self.assertEqual(self.get("a.txt"), b"a change\n")
        self.assertEqual(self.get("b.txt"), BASE_B)
        self.assertEqual(self.stash_list(), ["stash@{0}: On main: b-change"])
        self.git("reset", "--hard")
        self.git("stash", "pop", "stash@{0}")
        self.assertEqual(self.get("b.txt"), b"b change\n")
        self.assertEqual(self.stash_list(), [])

    def test_drop(self):
        for msg in ("m1", "m2", "m3"):
            self.put("a.txt", msg.encode() + b"\n")
            self.git("stash", "-m", msg)
        s3, s2, s1 = self.stash_shas()
        self.assertEqual(self.git("stash", "drop", "stash@{1}").out, f"Dropped stash@{{1}} ({s2})\n")
        self.assertEqual(self.stash_list(), ["stash@{0}: On main: m3", "stash@{1}: On main: m1"])
        self.assertEqual(self.stash_shas(), [s3, s1])
        self.assertEqual(self.git("stash", "drop").out, f"Dropped stash@{{0}} ({s3})\n")
        self.assertEqual(self.git("stash", "drop", "0").out, f"Dropped stash@{{0}} ({s1})\n")
        self.assertEqual(self.stash_list(), [])
        self.assertFalse(os.path.exists(self.stash_file()))
        self.assertEqual(self.get("a.txt"), BASE_A)  # drop never touches the worktree
        self.assertEqual(self.porcelain(), [])

    def test_empty_stash(self):
        self.assertEqual(self.git("stash", "list").out, "")
        for sub in ("pop", "apply", "drop"):
            self.assertIn("No stash entries found.", self.git_fail("stash", sub).err, sub)

    def test_bad_references_and_usage(self):
        self.put("a.txt", b"changed\n")
        self.git("stash")
        self.assertIn("stash@{3} does not exist", self.git_fail("stash", "drop", "stash@{3}").err)
        self.assertIn("stash@{1} does not exist", self.git_fail("stash", "pop", "1").err)
        usage = [
            ("stash", "drop", "banana"),
            ("stash", "drop", "stash@{x}"),
            ("stash", "apply", "stash@{-1}"),
            ("stash", "drop", "0", "1"),
            ("stash", "frobnicate"),
            ("stash", "save", "old syntax"),
            ("stash", "list", "extra"),
            ("stash", "pop", "-m", "x"),
            ("stash", "apply", "-u"),
            ("stash", "push", "a.txt"),
            ("stash", "push", "--", "a.txt"),
        ]
        for args in usage:
            res = self.git_fail(*args, code=2)
            self.assertIn("error:", res.err, args)
        self.assertEqual(self.stash_list(), [f"stash@{{0}}: {self.wip}"])
        self.assertEqual(self.get("a.txt"), BASE_A)

    def test_include_untracked_round_trip(self):
        self.put(".minigitignore", b"*.log\n")
        self.git("add", ".minigitignore")
        self.git("commit", "-m", "ignore logs")
        label = f"WIP on main: {self.head()[:7]} ignore logs"
        blob = bytes(range(256)) + b"\r\n\0end"
        self.put("new.txt", b"untracked\r\n")
        self.put("sub/deep/n.bin", blob)
        self.put("keep.log", b"ignored\n")
        self.put("a.txt", b"tracked change\n")
        res = self.git("stash", "push", "--include-untracked")
        self.assertEqual(res.out, f"Saved working directory and index state {label}\n")
        self.assertFalse(self.exists("new.txt"))
        self.assertFalse(self.exists("sub"))
        self.assertEqual(self.get("keep.log"), b"ignored\n")
        self.assertEqual(self.get("a.txt"), BASE_A)
        self.assertEqual(self.porcelain(), [])
        self.git("stash", "pop")
        self.assertEqual(self.get("new.txt"), b"untracked\r\n")
        self.assertEqual(self.get("sub/deep/n.bin"), blob)
        self.assertEqual(self.get("a.txt"), b"tracked change\n")
        self.assertEqual(self.porcelain(), [" M a.txt", "?? new.txt", "?? sub/deep/n.bin"])
        self.assertEqual(self.stash_list(), [])

    def test_untracked_file_already_present_refuses_and_keeps_entry(self):
        self.put("new.txt", b"stashed\n")
        self.put("a.txt", b"tracked change\n")
        self.git("stash", "-u")
        self.put("new.txt", b"mine\n")
        res = self.git_fail("stash", "pop")
        self.assertIn("already exist", res.err)
        self.assertIn("new.txt", res.err)
        self.assertEqual(self.get("new.txt"), b"mine\n")
        self.assertEqual(self.get("a.txt"), BASE_A)  # nothing was applied
        self.assertEqual(len(self.stash_list()), 1)
        os.remove(self.path("new.txt"))
        self.git("stash", "pop")
        self.assertEqual(self.get("new.txt"), b"stashed\n")
        self.assertEqual(self.get("a.txt"), b"tracked change\n")

    def test_pop_onto_a_moved_head_merges_cleanly(self):
        self.put("a.txt", b"ONE\ntwo\nthree\n")
        self.git("stash")
        self.git("switch", "-c", "topic")
        self.put("a.txt", b"one\ntwo\nTHREE\n")
        self.git("commit", "-a", "-m", "edit line three")
        self.put("b.txt", b"dirty, unrelated\n")
        self.git("stash", "pop")
        self.assertEqual(self.get("a.txt"), b"ONE\ntwo\nTHREE\n")
        self.assertEqual(self.get("b.txt"), b"dirty, unrelated\n")
        self.assertEqual(self.porcelain(), [" M a.txt", " M b.txt"])
        self.assertEqual(self.stash_list(), [])

    def test_pop_conflict_keeps_the_entry_until_dropped(self):
        self.put("a.txt", b"one\nstashed\nthree\n")
        self.git("stash")
        [sha] = self.stash_shas()
        self.put("a.txt", b"one\ncommitted\nthree\n")
        self.git("commit", "-a", "-m", "conflicting edit")
        res = self.git_fail("stash", "pop")
        self.assertIn("CONFLICT (content): Merge conflict in a.txt", res.out)
        self.assertIn("The stash entry is kept in case you need it again.", res.out)
        self.assertNotIn("Dropped", res.out)
        self.assertEqual(self.get("a.txt"),
                         b"one\n<<<<<<< Updated upstream\ncommitted\n=======\nstashed\n"
                         b">>>>>>> Stashed changes\nthree\n")
        self.assertEqual(self.porcelain(), ["UU a.txt"])
        self.assertEqual(self.stash_shas(), [sha])
        self.assertIn("unmerged paths", self.git_fail("stash").err)
        self.assertIn("unmerged paths", self.git_fail("stash", "apply").err)
        self.put("a.txt", b"one\nresolved\nthree\n")
        self.git("add", "a.txt")
        self.assertEqual(self.porcelain(), ["M  a.txt"])
        self.assertEqual(self.git("stash", "drop").out, f"Dropped stash@{{0}} ({sha})\n")
        self.git("commit", "-m", "resolved")
        self.assertEqual(self.porcelain(), [])

    def test_apply_refuses_to_overwrite_local_changes(self):
        self.put("a.txt", b"stashed\n")
        self.git("stash")
        self.put("a.txt", b"dirty again\n")
        res = self.git_fail("stash", "pop")
        self.assertIn("would be overwritten", res.err)
        self.assertIn("a.txt", res.err)
        self.assertEqual(self.get("a.txt"), b"dirty again\n")
        self.assertEqual(self.porcelain(), [" M a.txt"])
        self.assertEqual(len(self.stash_list()), 1)

    def test_bytes_round_trip_exactly(self):
        crlf = b"x\r\ny\r\n"
        binary = bytes(range(256))
        self.put("crlf.txt", crlf)
        self.put("bin.dat", binary)
        self.git("add", ".")
        self.git("commit", "-m", "bytes")
        new_crlf = b"x\r\nY\r\nno final newline"
        new_binary = bytes(reversed(range(256))) + b"\0\0"
        self.put("crlf.txt", new_crlf)
        self.put("bin.dat", new_binary)
        self.git("stash")
        self.assertEqual(self.get("crlf.txt"), crlf)
        self.assertEqual(self.get("bin.dat"), binary)
        self.git("stash", "pop")
        self.assertEqual(self.get("crlf.txt"), new_crlf)
        self.assertEqual(self.get("bin.dat"), new_binary)


class StashStorageTests(_StashCase):
    def test_entries_are_git_style_stash_commits(self):
        self.put("a.txt", b"tracked change\n")
        self.git("add", "a.txt")
        self.git("stash", "-m", "m1")
        self.put("new.txt", b"only untracked\n")
        self.git("stash", "-u", "-m", "with untracked")
        top, older = self.stash_shas()

        # W commit: parents = HEAD, index commit; tree = the stashed worktree.
        i_commit = self.parents_of(older)[1]
        self.assertEqual(self.parents_of(older), [self.base, i_commit])
        self.assertEqual(self.message_of(older), "On main: m1")
        self.assertEqual(self.parents_of(i_commit), [self.base])
        self.assertEqual(self.message_of(i_commit), f"index on main: {self.base[:7]} base")
        self.assertIn("\ta.txt", self.git("cat-file", "-p", self.tree_of(i_commit)).out)

        # With -u a third parent holds the untracked files (a root commit).
        parents = self.parents_of(top)
        self.assertEqual(len(parents), 3)
        self.assertEqual(parents[0], self.base)
        u_commit = parents[2]
        self.assertEqual(self.parents_of(u_commit), [])
        self.assertEqual(self.message_of(u_commit), f"untracked files on main: {self.base[:7]} base")
        self.assertEqual(self.git("cat-file", "-p", self.tree_of(u_commit)).out.count("\n"), 1)
        self.assertIn("\tnew.txt", self.git("cat-file", "-p", self.tree_of(u_commit)).out)

        # The stash is not a ref: history, branches and fsck are unaffected.
        self.assertFalse(self.exists(".minigit/refs/stash"))
        self.assertEqual(len(self.git("log", "--all", "--oneline").out.splitlines()), 1)
        self.assertEqual(self.git("branch", "-a").out, "* main\n")
        self.assertEqual(self.git("fsck").out, "")

    def test_corrupt_stash_file_is_reported(self):
        write(self.stash_file(), b"not a sha\n")
        self.assertIn("corrupt", self.git_fail("stash", "list").err)


class StashMultiUserTests(_StashCase):
    """The stash is local: it is never pushed, fetched or cloned."""

    def test_stash_stays_local_across_push_pull_and_clone(self):
        root = os.path.join(self.tmp, "srv")
        admin = make_env(os.path.join(self.tmp, "admin"))
        self.mg(self.tmp, "server", "init", root, env=admin)
        tokens = {u: self.mg(self.tmp, "server", "add-user", root, u, env=admin).out.strip()
                  for u in ("alice", "bob")}
        self.mg(self.tmp, "server", "create-repo", root, "proj", env=admin)
        server = ServerProcess(root, env=admin)
        server.start()
        self.addCleanup(server.stop)  # cleanups run LIFO: the server stops before tmp is removed
        url = f"{server.url}/proj"
        alice = make_env(os.path.join(self.tmp, "alice"), user="alice", token=tokens["alice"])
        bob = make_env(os.path.join(self.tmp, "bob"), user="bob", token=tokens["bob"])

        self.mg(self.tmp, "clone", url, "alice-wt", env=alice)
        awt = os.path.join(self.tmp, "alice-wt")
        write(os.path.join(awt, "a.txt"), BASE_A)
        self.mg(awt, "add", "a.txt", env=alice)
        self.mg(awt, "commit", "-m", "base", env=alice)
        self.mg(awt, "push", "-u", "origin", "main", env=alice)

        write(os.path.join(awt, "a.txt"), b"alice wip\n")
        write(os.path.join(awt, "secret.txt"), b"untracked secret\n")
        self.mg(awt, "stash", "-u", env=alice)
        self.assertIn("Everything up-to-date", self.mg(awt, "push", env=alice).text)

        self.mg(self.tmp, "clone", url, "bob-wt", env=bob)
        bwt = os.path.join(self.tmp, "bob-wt")
        self.assertEqual(read(os.path.join(bwt, "a.txt")), BASE_A)
        self.assertFalse(os.path.exists(os.path.join(bwt, "secret.txt")))
        self.assertEqual(self.mg(bwt, "stash", "list", env=bob).out, "")
        write(os.path.join(bwt, "c.txt"), b"bob\n")
        self.mg(bwt, "add", "c.txt", env=bob)
        self.mg(bwt, "commit", "-m", "bob adds c", env=bob)
        self.mg(bwt, "push", env=bob)

        self.mg(awt, "pull", env=alice)  # clean worktree thanks to the stash: fast-forward
        self.mg(awt, "stash", "pop", env=alice)
        self.assertEqual(read(os.path.join(awt, "a.txt")), b"alice wip\n")
        self.assertEqual(read(os.path.join(awt, "secret.txt")), b"untracked secret\n")
        self.assertEqual(read(os.path.join(awt, "c.txt")), b"bob\n")

        bare = os.path.join(root, "repos", "proj")
        self.assertFalse(os.path.exists(os.path.join(bare, "stash")))
        self.assertFalse(os.path.exists(os.path.join(bare, "refs", "stash")))
        self.assertEqual(self.mg(bare, "fsck", env=admin).out, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

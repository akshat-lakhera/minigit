"""tests.test_commands -- owner: M3 commands.

Unit tests for diff, status, history and ops, run against the real modules.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from minigit import diff as diffmod
from minigit import history, ops, status
from minigit.errors import (
    CheckoutConflictError, MinigitError, NothingToCommit, RevisionError, UsageError,
)
from minigit.index import IndexEntry, locked_index, read_index
from minigit.objects import EMPTY_TREE, hash_object
from minigit.repo import Repository

ENV_KEYS = ("MINIGIT_GLOBAL_CONFIG", "MINIGIT_AUTHOR_NAME", "MINIGIT_AUTHOR_EMAIL", "MINIGIT_AUTHOR_DATE")


class RepoCase(unittest.TestCase):
    """A temp repo with a fixed identity and date."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["MINIGIT_GLOBAL_CONFIG"] = os.path.join(self.root, "globalconfig")
        os.environ["MINIGIT_AUTHOR_NAME"] = "Test User"
        os.environ["MINIGIT_AUTHOR_EMAIL"] = "test@example.com"
        os.environ["MINIGIT_AUTHOR_DATE"] = "1700000000 +0000"
        self.clock = 1700000000
        self.wt = os.path.join(self.root, "wt")
        os.makedirs(self.wt)
        ops.init(self.wt)
        self.repo = Repository.open(self.wt)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # helpers -------------------------------------------------------------
    def write(self, rel: str, data: bytes | str) -> None:
        p = os.path.join(self.wt, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if isinstance(data, str):
            data = data.encode()
        with open(p, "wb") as f:
            f.write(data)

    def read(self, rel: str) -> bytes:
        with open(os.path.join(self.wt, *rel.split("/")), "rb") as f:
            return f.read()

    def exists(self, rel: str) -> bool:
        return os.path.lexists(os.path.join(self.wt, *rel.split("/")))

    def add(self, *specs: str, force: bool = False):
        return ops.add(self.repo, list(specs) or ["."], self.wt, force=force)

    def commit(self, msg: str, **kw) -> str:
        self.clock += 60
        os.environ["MINIGIT_AUTHOR_DATE"] = f"{self.clock} +0000"
        sha, _ = ops.commit(self.repo, [msg], **kw)
        return sha

    def commit_file(self, rel: str, data, msg: str | None = None) -> str:
        self.write(rel, data)
        self.add(rel)
        return self.commit(msg or f"edit {rel}")

    def porcelain(self) -> str:
        return status.format_status_porcelain(status.compute_status(self.repo))

    def diff(self, *revs, staged=False, paths=(), **kw):
        return diffmod.diff_command(self.repo, self.wt, staged=staged, revs=list(revs), pathspecs=list(paths), **kw)

    def merge(self, branch: str, msg: str, *, no_ff: bool = False) -> str:
        """Merge ``branch`` into the current branch with the next clock tick; returns the new HEAD."""
        from minigit import merge as mergemod
        self.clock += 60
        os.environ["MINIGIT_AUTHOR_DATE"] = f"{self.clock} +0000"
        outcome = mergemod.merge(self.repo, branch, message=msg, no_ff=no_ff)
        self.assertIn(outcome.kind, ("merged", "fast-forward"))
        return self.repo.head_commit()

    def date(self, sha: str) -> str:
        from minigit.util import format_date
        c = self.repo.read_commit(sha)
        return format_date(c.author.timestamp, c.author.tz_offset)


# =========================================================================== init / add / rm

class InitAddRmTests(RepoCase):
    def test_init_messages(self):
        other = os.path.join(self.root, "other")
        self.assertTrue(ops.init(other).startswith("Initialized empty minigit repository in"))
        self.assertTrue(ops.init(other).startswith("Reinitialized existing"))
        self.assertTrue(os.path.isfile(os.path.join(other, ".minigit", "HEAD")))
        bare = os.path.join(self.root, "bare")
        ops.init(bare, bare=True, initial_branch="trunk")
        with open(os.path.join(bare, "HEAD")) as f:
            self.assertEqual(f.read().strip(), "ref: refs/heads/trunk")

    def test_add_file_dir_and_dot(self):
        self.write("a.txt", "a\n")
        self.write("sub/b.txt", "b\n")
        self.write("sub/deep/c.txt", "c\n")
        self.assertEqual(self.add("a.txt"), ["a.txt"])
        self.assertEqual(self.add("sub"), ["sub/b.txt", "sub/deep/c.txt"])
        self.assertEqual(sorted(read_index(self.repo).paths()), ["a.txt", "sub/b.txt", "sub/deep/c.txt"])
        # unchanged files are not re-reported by a directory add
        self.assertEqual(self.add("."), [])
        self.write("sub/b.txt", "b2\n")
        self.assertEqual(self.add("."), ["sub/b.txt"])

    def test_add_relative_to_cwd(self):
        self.write("sub/b.txt", "b\n")
        got = ops.add(self.repo, ["b.txt"], os.path.join(self.wt, "sub"))
        self.assertEqual(got, ["sub/b.txt"])

    def test_add_all_stages_the_whole_tree_from_any_directory(self):
        self.commit_file("a.txt", "a\n")
        self.commit_file("sub/b.txt", "b\n")
        self.write("a.txt", "a2\n")
        os.unlink(os.path.join(self.wt, "sub", "b.txt"))
        self.write("new.txt", "n\n")
        self.write("sub/deep/c.txt", "c\n")
        # -A with no pathspec covers the root, whatever cwd is (here: a subdirectory)
        got = ops.add(self.repo, [], os.path.join(self.wt, "sub"), all_=True)
        self.assertEqual(got, ["a.txt", "new.txt", "sub/b.txt", "sub/deep/c.txt"])
        self.assertEqual(sorted(read_index(self.repo).paths()), ["a.txt", "new.txt", "sub/deep/c.txt"])
        self.assertEqual(self.porcelain(), "M  a.txt\nA  new.txt\nD  sub/b.txt\nA  sub/deep/c.txt\n")
        # a second -A has nothing left to do
        self.assertEqual(ops.add(self.repo, [], self.wt, all_=True), [])

    def test_add_update_stages_tracked_files_only(self):
        self.commit_file("a.txt", "a\n")
        self.commit_file("sub/b.txt", "b\n")
        self.commit_file("sub/c.txt", "c\n")
        self.write("a.txt", "a2\n")
        self.write("sub/c.txt", "c2\n")
        os.unlink(os.path.join(self.wt, "sub", "b.txt"))
        self.write("new.txt", "n\n")
        self.write("sub/new.txt", "n\n")
        # with a pathspec: only the tracked files under it
        self.assertEqual(ops.add(self.repo, ["sub"], self.wt, update=True), ["sub/b.txt", "sub/c.txt"])
        self.assertEqual(self.porcelain(), " M a.txt\n?? new.txt\nD  sub/b.txt\nM  sub/c.txt\n?? sub/new.txt\n")
        # without: the whole tree, new files are still never staged
        self.assertEqual(ops.add(self.repo, [], os.path.join(self.wt, "sub"), update=True), ["a.txt"])
        self.assertEqual(self.porcelain(), "M  a.txt\n?? new.txt\nD  sub/b.txt\nM  sub/c.txt\n?? sub/new.txt\n")
        self.assertEqual(sorted(read_index(self.repo).paths()), ["a.txt", "sub/c.txt"])
        # an untracked file or an unknown name is not a match for -u
        for spec in ("new.txt", "nope"):
            with self.assertRaises(MinigitError) as cm:
                ops.add(self.repo, [spec], self.wt, update=True)
            self.assertEqual(cm.exception.message, f"pathspec '{spec}' did not match any files")
        with self.assertRaises(UsageError):
            ops.add(self.repo, [], self.wt, all_=True, update=True)
        with self.assertRaises(UsageError):
            ops.add(self.repo, [], self.wt)

    def test_add_removes_missing_tracked(self):
        self.commit_file("gone.txt", "x\n")
        os.unlink(os.path.join(self.wt, "gone.txt"))
        self.assertEqual(self.add("gone.txt"), ["gone.txt"])
        self.assertEqual(read_index(self.repo).paths(), [])
        self.commit_file("d/one.txt", "1\n")
        self.commit_file("d/two.txt", "2\n")
        import shutil
        shutil.rmtree(os.path.join(self.wt, "d"))
        self.assertEqual(self.add("."), ["d/one.txt", "d/two.txt"])
        self.assertEqual(read_index(self.repo).paths(), [])

    def test_add_no_match_and_ignored(self):
        with self.assertRaises(MinigitError) as cm:
            self.add("nope.txt")
        self.assertEqual(cm.exception.message, "pathspec 'nope.txt' did not match any files")
        self.write(".minigitignore", "*.log\n")
        self.write("x.log", "log\n")
        with self.assertRaises(MinigitError) as cm:
            self.add("x.log")
        self.assertEqual(cm.exception.message, "path 'x.log' is ignored by .minigitignore (use -f)")
        self.assertEqual(self.add("x.log", force=True), ["x.log"])
        # directory add skips ignored files but keeps tracked ones
        self.write("y.log", "y\n")
        self.write("z.txt", "z\n")
        self.assertEqual(self.add("."), [".minigitignore", "z.txt"])
        self.write("x.log", "changed\n")
        self.assertEqual(self.add("."), ["x.log"])

    def test_add_force_on_ignored_directory_stages_its_files(self):
        self.write(".minigitignore", "private/\n*.log\n")
        self.write("private/a.txt", "a\n")
        self.write("private/deep/b.txt", "b\n")
        self.write("x.log", "l\n")
        with self.assertRaises(MinigitError) as cm:
            self.add("private")
        self.assertIn("use -f", cm.exception.message)
        self.assertEqual(self.add("private", force=True), ["private/a.txt", "private/deep/b.txt"])
        self.assertEqual(self.add(".", force=True), [".minigitignore", "x.log"])
        self.assertEqual(read_index(self.repo).paths(),
                         [".minigitignore", "private/a.txt", "private/deep/b.txt", "x.log"])

    def test_add_never_stages_the_gitdir(self):
        for spec in (".minigit", ".minigit/HEAD", "./.minigit"):
            for force in (False, True):
                with self.subTest(spec=spec, force=force):
                    with self.assertRaises(MinigitError):
                        self.add(spec, force=force)
        self.assertEqual(read_index(self.repo).paths(), [])
        self.write("f.txt", "f\n")
        self.assertEqual(self.add(".", force=True), ["f.txt"])

    def test_add_is_atomic(self):
        self.write("ok.txt", "ok\n")
        with self.assertRaises(MinigitError):
            self.add("ok.txt", "missing.txt")
        self.assertEqual(read_index(self.repo).paths(), [])
        self.assertFalse(os.path.exists(self.repo.gpath("index.lock")))

    def test_add_clears_conflict_stages(self):
        self.commit_file("c.txt", "base\n")
        with locked_index(self.repo) as index:
            sha = self.repo.objects.write("blob", b"ours\n")
            index.put(IndexEntry("c.txt", "100644", sha, stage=2))
            index.put(IndexEntry("c.txt", "100644", hash_object("blob", b"theirs\n"), stage=3))
        self.assertIn("AA c.txt", self.porcelain())
        self.write("c.txt", "resolved\n")
        self.add("c.txt")
        self.assertEqual(read_index(self.repo).unmerged(), {})
        self.assertEqual(self.porcelain(), "M  c.txt\n")

    def test_rm_variants(self):
        self.commit_file("a.txt", "a\n")
        self.commit_file("d/b.txt", "b\n")
        self.assertEqual(ops.rm(self.repo, ["a.txt"], self.wt), ["rm 'a.txt'"])
        self.assertFalse(self.exists("a.txt"))
        self.assertEqual(self.porcelain(), "D  a.txt\n")
        with self.assertRaises(MinigitError) as cm:
            ops.rm(self.repo, ["d"], self.wt)
        self.assertIn("without -r", cm.exception.message)
        self.assertEqual(ops.rm(self.repo, ["d"], self.wt, recursive=True), ["rm 'd/b.txt'"])
        self.assertFalse(self.exists("d"))  # empty dir pruned
        with self.assertRaises(MinigitError) as cm:
            ops.rm(self.repo, ["zzz"], self.wt)
        self.assertIn("did not match", cm.exception.message)

    def test_rm_refuses_changes_unless_forced(self):
        self.commit_file("a.txt", "a\n")
        self.write("a.txt", "modified\n")
        with self.assertRaises(MinigitError) as cm:
            ops.rm(self.repo, ["a.txt"], self.wt)
        self.assertEqual(cm.exception.message, "'a.txt' has staged or local changes (use -f)")
        # --cached is fine while the index still equals HEAD
        ops.rm(self.repo, ["a.txt"], self.wt, cached=True)
        self.assertTrue(self.exists("a.txt"))
        self.assertEqual(self.porcelain(), "?? a.txt\nD  a.txt\n")  # sorted by (path, XY)
        # a staged new file is refused without -f
        self.write("n.txt", "n\n")
        self.add("n.txt")
        with self.assertRaises(MinigitError):
            ops.rm(self.repo, ["n.txt"], self.wt)
        ops.rm(self.repo, ["n.txt"], self.wt, force=True)
        self.assertFalse(self.exists("n.txt"))


# =========================================================================== status

class StatusTests(RepoCase):
    def test_unborn_and_clean(self):
        st = status.compute_status(self.repo)
        self.assertTrue(st.unborn)
        self.assertTrue(st.is_clean())
        text = status.format_status_long(st)
        self.assertIn("On branch main\n\nNo commits yet\n", text)
        self.assertIn("nothing to commit, working tree clean", text)
        self.commit_file("a.txt", "a\n")
        text = status.format_status_long(status.compute_status(self.repo))
        self.assertEqual(text, "On branch main\nnothing to commit, working tree clean\n")

    def test_every_code(self):
        self.commit_file("mod.txt", "1\n")
        self.commit_file("del.txt", "1\n")
        self.commit_file("both.txt", "1\n")
        self.commit_file("wdel.txt", "1\n")
        self.commit_file("staged_del_kept.txt", "1\n")
        self.commit_file("mod2.txt", "x\n")
        self.write("mod2.txt", "y\n")            # " M"
        self.write("mod.txt", "2\n")
        self.add("mod.txt")                      # M (index vs HEAD)
        ops.rm(self.repo, ["del.txt"], self.wt)  # D
        self.write("both.txt", "2\n")
        self.add("both.txt")
        self.write("both.txt", "3\n")            # MM
        os.unlink(os.path.join(self.wt, "wdel.txt"))  # " D"
        ops.rm(self.repo, ["staged_del_kept.txt"], self.wt, cached=True)  # "D " + "??"
        self.write("new.txt", "n\n")
        self.add("new.txt")
        self.write("new.txt", "n2\n")            # AM
        self.write("added.txt", "a\n")
        self.add("added.txt")                    # "A "
        self.write("untracked/u.txt", "u\n")     # ??
        self.assertEqual(self.porcelain(),
                         "A  added.txt\n"
                         "MM both.txt\n"
                         "D  del.txt\n"
                         "M  mod.txt\n"
                         " M mod2.txt\n"
                         "AM new.txt\n"
                         "?? staged_del_kept.txt\n"
                         "D  staged_del_kept.txt\n"
                         "?? untracked/u.txt\n"
                         " D wdel.txt\n")
        text = status.format_status_long(status.compute_status(self.repo))
        self.assertIn("Changes to be committed:\n\tnew file:   added.txt\n", text)
        self.assertIn("\tmodified:   mod.txt\n", text)
        self.assertIn("\tdeleted:    del.txt\n", text)
        self.assertIn("Changes not staged for commit:\n", text)
        self.assertIn("\tdeleted:    wdel.txt\n", text)
        self.assertIn("Untracked files:\n\tstaged_del_kept.txt\n\tuntracked/u.txt\n", text)
        self.assertNotIn("nothing to commit", text)

    def test_unmerged_codes(self):
        self.commit_file("f.txt", "1\n")
        b = hash_object("blob", b"b\n")
        o = hash_object("blob", b"o\n")
        t = hash_object("blob", b"t\n")
        cases = {
            "uu.txt": ({1: b, 2: o, 3: t}, "UU", "both modified:"),
            "aa.txt": ({2: o, 3: t}, "AA", "both added:"),
            "ud.txt": ({1: b, 2: o}, "UD", "deleted by them:"),
            "du.txt": ({1: b, 3: t}, "DU", "deleted by us:"),
            "au.txt": ({2: o}, "AU", "added by us:"),
            "ua.txt": ({3: t}, "UA", "added by them:"),
        }
        with locked_index(self.repo) as index:
            for path, (stages, _, _) in cases.items():
                for stage, sha in stages.items():
                    index.put(IndexEntry(path, "100644", sha, stage=stage))
        self.repo.write_state("MERGE_HEAD", b"0" * 40 + b"\n")
        st = status.compute_status(self.repo)
        self.assertTrue(st.merging)
        codes = dict((p, xy) for xy, p in st.entries)
        for path, (_, xy, _) in cases.items():
            self.assertEqual(codes[path], xy, path)
        text = status.format_status_long(st)
        self.assertIn("You have unmerged paths.", text)
        for path, (_, _, label) in cases.items():
            self.assertIn(f"\t{label:<17}{path}\n", text)
        with locked_index(self.repo) as index:
            for path in cases:
                index.remove(path)
        text = status.format_status_long(status.compute_status(self.repo))
        self.assertIn("All conflicts fixed but you are still merging.", text)

    def test_ignored_and_nested_ignore(self):
        self.write(".minigitignore", "*.log\nbuild/\n")
        self.write("a.log", "x")
        self.write("build/out.bin", "x")
        self.write("keep.txt", "k")
        self.assertEqual(self.porcelain(), "?? .minigitignore\n?? keep.txt\n")

    def test_upstream_gone(self):
        self.commit_file("a.txt", "1\n")
        ops.set_upstream(self.repo, "main", "origin", "refs/heads/main")   # no refs/remotes/origin/main
        st = status.compute_status(self.repo)
        self.assertEqual((st.upstream, st.upstream_gone, st.ahead, st.behind), ("origin/main", True, 0, 0))
        self.assertEqual(status.format_status_long(st),
                         "On branch main\n"
                         "Your branch is based on 'origin/main', but the upstream is gone.\n"
                         '  (use "minigit branch --unset-upstream" to fixup)\n'
                         "\nnothing to commit, working tree clean\n")
        self.write("b.txt", "b")
        st = status.compute_status(self.repo)
        self.assertEqual(status.format_status_porcelain(st), "?? b.txt\n")   # porcelain: entries only
        self.assertIn("but the upstream is gone.\n", status.format_status_long(st))
        ops.unset_upstream(self.repo)
        st = status.compute_status(self.repo)
        self.assertEqual((st.upstream, st.upstream_gone), (None, False))
        self.assertNotIn("upstream", status.format_status_long(st))
        # an unborn branch whose upstream is gone says so too, after "No commits yet"
        ops.switch(self.repo, None, create="orphan")
        ops.set_upstream(self.repo, "orphan", "origin", "refs/heads/orphan")
        self.repo.refs.update("refs/heads/orphan", None, expected=self.repo.head_commit())
        text = status.format_status_long(status.compute_status(self.repo))
        self.assertIn("No commits yet\nYour branch is based on 'origin/orphan', but the upstream is gone.\n", text)

    def test_detached_and_upstream(self):
        c1 = self.commit_file("a.txt", "1\n")
        c2 = self.commit_file("a.txt", "2\n")
        self.repo.refs.update("refs/remotes/origin/main", c1)
        ops.set_upstream(self.repo, "main", "origin", "refs/heads/main")
        st = status.compute_status(self.repo)
        self.assertEqual((st.upstream, st.ahead, st.behind), ("origin/main", 1, 0))
        self.assertIn("Your branch is ahead of 'origin/main' by 1 commit.\n",
                      status.format_status_long(st))
        self.repo.refs.update("refs/remotes/origin/main", c2)
        self.repo.refs.update("refs/heads/main", c1)
        st = status.compute_status(self.repo)
        self.assertEqual((st.ahead, st.behind), (0, 1))
        self.assertIn("behind 'origin/main' by 1 commit, and can be fast-forwarded.",
                      status.format_status_long(st))
        # restore the worktree so that the detach below is clean
        ops.reset(self.repo, "HEAD", "hard")
        ops.switch(self.repo, c2, detach=True)
        st = status.compute_status(self.repo)
        self.assertTrue(st.detached)
        self.assertIsNone(st.branch)
        self.assertTrue(status.format_status_long(st).startswith(f"HEAD detached at {c2[:7]}\n"))


# =========================================================================== diff

class DiffTests(RepoCase):
    def test_render_hunks_and_ranges(self):
        old = diffmod.FileVersion("100644", hash_object("blob", b"a\nb\nc\n"), b"a\nb\nc\n")
        new = diffmod.FileVersion("100644", hash_object("blob", b"a\nB\nc\n"), b"a\nB\nc\n")
        out = diffmod.render_file_diff("f.txt", old, new).decode()
        self.assertEqual(out,
                         "diff --git a/f.txt b/f.txt\n"
                         f"index {old.sha[:7]}..{new.sha[:7]} 100644\n"
                         "--- a/f.txt\n+++ b/f.txt\n"
                         "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n")
        # single-line file: ",l" omitted when l == 1; l == 0 uses start = i1
        one = diffmod.FileVersion("100644", hash_object("blob", b"x\n"), b"x\n")
        gone = diffmod.render_file_diff("f.txt", one, None).decode()
        self.assertIn("deleted file mode 100644\n", gone)
        self.assertIn(f"index {one.sha[:7]}..0000000\n--- a/f.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n", gone)
        born = diffmod.render_file_diff("f.txt", None, one).decode()
        self.assertIn("new file mode 100644\n", born)
        self.assertIn(f"index 0000000..{one.sha[:7]}\n--- /dev/null\n+++ b/f.txt\n@@ -0,0 +1 @@\n+x\n", born)

    def test_no_newline_binary_mode_only(self):
        a = diffmod.FileVersion("100644", hash_object("blob", b"a\nb"), b"a\nb")
        b = diffmod.FileVersion("100644", hash_object("blob", b"a\nb\n"), b"a\nb\n")
        out = diffmod.render_file_diff("f", a, b).decode()
        self.assertTrue(out.endswith("@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+b\n"))
        bin1 = diffmod.FileVersion("100644", hash_object("blob", b"\0\1\2"), b"\0\1\2")
        out = diffmod.render_file_diff("f", a, bin1).decode()
        self.assertEqual(out, f"diff --git a/f b/f\nindex {a.sha[:7]}..{bin1.sha[:7]} 100644\n"
                              "Binary files a/f and b/f differ\n")
        self.assertNotIn("---", out)
        exe = diffmod.FileVersion("100755", a.sha, a.data)
        out = diffmod.render_file_diff("f", a, exe).decode()
        self.assertEqual(out, "diff --git a/f b/f\nold mode 100644\nnew mode 100755\n")
        self.assertEqual(diffmod.render_file_diff("f", a, a), b"")
        # mode and content change: no " <mode>" on the index line
        c = diffmod.FileVersion("100755", b.sha, b.data)
        self.assertIn(f"index {a.sha[:7]}..{b.sha[:7]}\n", diffmod.render_file_diff("f", a, c).decode())

    def test_changed_paths_filter(self):
        old = {"a": ("100644", "1"), "d/x": ("100644", "1"), "dd": ("100644", "1")}
        new = {"a": ("100644", "2"), "d/x": ("100644", "2"), "dd": ("100644", "2"), "n": ("100644", "3")}
        self.assertEqual(diffmod.changed_paths(old, new), ["a", "d/x", "dd", "n"])
        self.assertEqual(diffmod.changed_paths(old, new, ["d"]), ["d/x"])
        self.assertEqual(diffmod.changed_paths(old, new, ["dd", "n"]), ["dd", "n"])

    def test_four_forms(self):
        c1 = self.commit_file("f.txt", "one\ntwo\n")
        self.write("f.txt", "one\ntwo\nthree\n")
        self.add("f.txt")
        c2 = self.commit("c2")
        self.write("f.txt", "one\ntwo\nthree\nfour\n")
        self.add("f.txt")
        self.write("f.txt", "zero\none\ntwo\nthree\nfour\n")
        self.write("untracked.txt", "u\n")
        wt_vs_index = self.diff().decode()
        self.assertIn("@@ -1,3 +1,4 @@\n+zero\n one\n two\n three\n", wt_vs_index)
        self.assertNotIn("untracked", wt_vs_index)
        staged = self.diff(staged=True).decode()
        self.assertIn("@@ -1,3 +1,4 @@\n one\n two\n three\n+four\n", staged)
        rev_vs_wt = self.diff(c1).decode()
        self.assertIn("@@ -1,2 +1,5 @@\n+zero\n one\n two\n+three\n+four\n", rev_vs_wt)
        two = self.diff(c1, c2).decode()
        self.assertIn("@@ -1,2 +1,3 @@\n one\n two\n+three\n", two)
        self.assertEqual(self.diff(f"{c1}..{c2}"), self.diff(c1, c2))
        self.assertEqual(self.diff("HEAD~1", "HEAD"), self.diff(c1, c2))
        self.assertEqual(self.diff(c1, c2, paths=["other"]), b"")
        self.assertEqual(self.diff(c1, c2, paths=["f.txt"]), self.diff(c1, c2))
        with self.assertRaises(UsageError):
            self.diff(c1, c2, "HEAD")

    def test_worktree_diff_shows_deletion_and_crlf(self):
        self.commit_file("crlf.txt", b"a\r\nb\r\n")
        self.commit_file("gone.txt", b"g\n")
        self.write("crlf.txt", b"a\r\nB\r\n")
        os.unlink(os.path.join(self.wt, "gone.txt"))
        out = self.diff()
        self.assertIn(b"-b\r\n+B\r\n", out)
        self.assertIn(b"deleted file mode 100644\n", out)
        self.assertIn(b"+++ /dev/null\n@@ -1 +0,0 @@\n-g\n", out)
        self.assertEqual(self.read("crlf.txt"), b"a\r\nB\r\n")

    def test_unmerged_path_line(self):
        self.commit_file("c.txt", "1\n")
        with locked_index(self.repo) as index:
            index.put(IndexEntry("c.txt", "100644", hash_object("blob", b"o\n"), stage=2))
            index.put(IndexEntry("c.txt", "100644", hash_object("blob", b"t\n"), stage=3))
        self.assertEqual(self.diff(), b"* Unmerged path c.txt\n")
        self.assertEqual(self.diff(staged=True), b"* Unmerged path c.txt\n")

    def test_binary_through_command(self):
        self.commit_file("img.bin", b"\0\1\2\3")
        self.write("img.bin", b"\0\1\2\4")
        self.assertIn(b"Binary files a/img.bin and b/img.bin differ\n", self.diff())


    def test_diffstat_format(self):
        from minigit.diff import FileStat, format_diffstat, format_shortstat
        self.assertEqual(format_diffstat([]), "")
        self.assertEqual(format_diffstat([FileStat("a.txt", 1, 0)]),
                         " a.txt | 1 +\n 1 file changed, 1 insertion(+)\n")
        self.assertEqual(format_diffstat([FileStat("a.txt", 0, 1)]),
                         " a.txt | 1 -\n 1 file changed, 1 deletion(-)\n")
        # the name column is padded to the longest name, counts are right-aligned
        self.assertEqual(format_diffstat([FileStat("a.txt", 2, 3), FileStat("dir/long.txt", 10, 0)]),
                         " a.txt        |  5 ++---\n"
                         " dir/long.txt | 10 ++++++++++\n"
                         " 2 files changed, 12 insertions(+), 3 deletions(-)\n")
        # the bar is scaled so the line fits in 80 columns (git's rules)
        big = format_diffstat([FileStat("big.txt", 200, 100)])
        self.assertEqual(big, " big.txt | 300 " + "+" * 42 + "-" * 22 + "\n"
                              " 1 file changed, 200 insertions(+), 100 deletions(-)\n")
        self.assertTrue(all(len(line) < 80 for line in big.splitlines()))
        # binary files: sizes instead of a bar, and the counts line up with "Bin"
        self.assertEqual(format_diffstat([FileStat("a.txt", 1, 0), FileStat("img.bin", 5, 3, binary=True)]),
                         " a.txt   |   1 +\n"
                         " img.bin | Bin 3 -> 5 bytes\n"
                         " 2 files changed, 1 insertion(+)\n")
        self.assertEqual(format_diffstat([FileStat("e", 0, 0)]),
                         " e | 0\n 1 file changed, 0 insertions(+), 0 deletions(-)\n")
        # a too-long name is cut at a directory boundary
        long_name = "d" * 100 + "/f.txt"
        self.assertEqual(format_diffstat([FileStat(long_name, 1, 0)]).splitlines()[0],
                         " .../f.txt" + " " * 63 + " | 1 +")
        # summary singular/plural and omitted zero parts
        self.assertEqual(format_shortstat(1, 1, 1), " 1 file changed, 1 insertion(+), 1 deletion(-)")
        self.assertEqual(format_shortstat(2, 0, 0), " 2 files changed, 0 insertions(+), 0 deletions(-)")
        self.assertEqual(format_shortstat(1, 0, 2), " 1 file changed, 2 deletions(-)")
        self.assertEqual(format_shortstat(3, 4, 0), " 3 files changed, 4 insertions(+)")

    def test_diffstat_rename_label(self):
        from minigit.diff import _rename_name
        self.assertEqual(_rename_name("a.txt", "b.txt"), "a.txt => b.txt")
        self.assertEqual(_rename_name("dir/a.txt", "dir/b.txt"), "dir/{a.txt => b.txt}")
        self.assertEqual(_rename_name("a/x.txt", "b/x.txt"), "{a => b}/x.txt")
        self.assertEqual(_rename_name("x", "dir/x"), "x => dir/x")
        self.assertEqual(_rename_name("a/b/c", "a/c"), "a/{b => }/c")

    def test_diff_stat_forms(self):
        self.commit_file("a.txt", "1\n", "one")
        self.write("a.txt", "1\n2\n")
        self.write("b.bin", b"\0\1")
        self.add("b.bin")
        self.assertEqual(self.diff(stat=True).decode(), " a.txt | 1 +\n 1 file changed, 1 insertion(+)\n")
        self.assertEqual(self.diff(staged=True, stat=True).decode(),
                         " b.bin | Bin 0 -> 2 bytes\n 1 file changed, 0 insertions(+), 0 deletions(-)\n")
        both = (" a.txt |   1 +\n b.bin | Bin 0 -> 2 bytes\n 2 files changed, 1 insertion(+)\n")
        self.assertEqual(self.diff("HEAD", stat=True).decode(), both)
        self.add("a.txt")
        self.commit("two")
        self.assertEqual(self.diff("HEAD~1", "HEAD", stat=True).decode(), both)
        self.assertEqual(self.diff("HEAD~1", "HEAD", stat=True, paths=["a.txt"]).decode(),
                         " a.txt | 1 +\n 1 file changed, 1 insertion(+)\n")
        self.assertEqual(self.diff(stat=True), b"")
        # an exact rename is one file with no lines, shown at its new path
        ops.rm(self.repo, ["a.txt"], self.wt)
        self.write("c.txt", "1\n2\n")
        self.add("c.txt")
        self.assertEqual(self.diff(staged=True, stat=True).decode(),
                         " a.txt => c.txt | 0\n 1 file changed, 0 insertions(+), 0 deletions(-)\n")
        # the commit summary line comes from the same counting
        _sha, summary = ops.commit(self.repo, ["three"], stat=True)
        self.assertTrue(summary.endswith("\n 1 file changed, 0 insertions(+), 0 deletions(-)"), summary)


# =========================================================================== commit

class CommitTests(RepoCase):
    def test_root_and_second_commit(self):
        self.write("a.txt", "a\n")
        self.add("a.txt")
        sha, summary = ops.commit(self.repo, ["first", "body"])
        self.assertEqual(summary, f"[main (root-commit) {sha[:7]}] first")
        c = self.repo.read_commit(sha)
        self.assertEqual(c.message, "first\n\nbody\n")
        self.assertEqual(c.parents, ())
        self.assertEqual(c.author.name, "Test User")
        self.assertEqual(c.author.timestamp, 1700000000)
        self.assertEqual(self.repo.head_commit(), sha)
        self.write("a.txt", "b\n")
        self.add("a.txt")
        sha2, summary2 = ops.commit(self.repo, ["second  \n\n"])
        self.assertEqual(summary2, f"[main {sha2[:7]}] second")
        self.assertEqual(self.repo.read_commit(sha2).parents, (sha,))
        self.assertEqual(self.repo.read_commit(sha2).message, "second\n")

    def test_nothing_to_commit_and_allow_empty(self):
        with self.assertRaises(NothingToCommit):
            ops.commit(self.repo, ["x"])
        self.commit_file("a.txt", "a\n")
        with self.assertRaises(NothingToCommit) as cm:
            ops.commit(self.repo, ["x"])
        self.assertEqual(cm.exception.message, "nothing to commit, working tree clean")
        sha, summary = ops.commit(self.repo, ["empty"], allow_empty=True, stat=True)
        self.assertEqual(self.repo.head_commit(), sha)
        self.assertEqual(summary, f"[main {sha[:7]}] empty")   # no stat line when nothing changed

    def test_summary_has_the_git_stat_line(self):
        def commit(msg):
            return ops.commit(self.repo, [msg], stat=True)[1].split("\n", 1)[1]

        self.write("a.txt", "1\n2\n3\n")
        self.write("b.txt", "b\n")
        self.add("a.txt", "b.txt")
        sha, summary = ops.commit(self.repo, ["root"], stat=True)
        self.assertEqual(summary, f"[main (root-commit) {sha[:7]}] root\n 2 files changed, 4 insertions(+)")
        self.write("b.txt", "b\nmore\n")
        self.add("b.txt")
        self.assertEqual(commit("one more line"), " 1 file changed, 1 insertion(+)")
        self.write("a.txt", "1\ntwo\n3\n4\n5\n")
        self.add("a.txt")
        self.assertEqual(commit("edit"), " 1 file changed, 3 insertions(+), 1 deletion(-)")
        self.write("a.txt", "1\ntwo\n3\n4\n5")    # only the final newline goes
        self.add("a.txt")
        self.assertEqual(commit("no eol"), " 1 file changed, 1 insertion(+), 1 deletion(-)")
        os.unlink(os.path.join(self.wt, "b.txt"))
        self.write("c.txt", "b\nmore\n")
        self.add("b.txt", "c.txt")
        self.assertEqual(commit("rename"), " 1 file changed, 0 insertions(+), 0 deletions(-)")
        os.unlink(os.path.join(self.wt, "a.txt"))
        self.add("a.txt")
        self.assertEqual(commit("delete"), " 1 file changed, 5 deletions(-)")
        self.write("img.bin", b"\0\1\2")
        self.add("img.bin")
        self.assertEqual(commit("binary"), " 1 file changed, 0 insertions(+), 0 deletions(-)")
        # concluding a merge prints no stat line, as in git
        self.repo.refs.write_special("MERGE_HEAD", self.repo.read_commit(self.repo.head_commit()).parents[0])
        self.write("m.txt", "m\n")
        self.add("m.txt")
        sha, summary = ops.commit(self.repo, ["merge"], stat=True)
        self.assertEqual(summary, f"[main {sha[:7]}] merge")
        self.assertEqual(len(self.repo.read_commit(sha).parents), 2)

    def test_nothing_to_commit_wording_matches_the_worktree(self):
        self.commit_file("a.txt", "a\n")
        self.write("a.txt", "changed\n")
        with self.assertRaises(NothingToCommit) as cm:
            ops.commit(self.repo, ["x"])
        self.assertEqual(cm.exception.message,
                         'no changes added to commit (use "minigit add" and/or "minigit commit -a")')
        os.unlink(os.path.join(self.wt, "a.txt"))
        with self.assertRaises(NothingToCommit) as cm:
            ops.commit(self.repo, ["x"])
        self.assertTrue(cm.exception.message.startswith("no changes added to commit"))
        self.write("a.txt", "a\n")
        self.write("new.txt", "n\n")
        with self.assertRaises(NothingToCommit) as cm:
            ops.commit(self.repo, ["x"])
        self.assertEqual(cm.exception.message,
                         'nothing added to commit but untracked files present (use "minigit add" to track)')

    def test_message_rules(self):
        self.write("a.txt", "a\n")
        self.add("a.txt")
        with self.assertRaises(UsageError) as cm:
            ops.commit(self.repo, [])
        self.assertEqual(cm.exception.message, "commit message required: use -m")
        with self.assertRaises(MinigitError) as cm:
            ops.commit(self.repo, ["   \n"])
        self.assertEqual(cm.exception.message, "aborting commit due to empty commit message")
        self.repo.write_state("MERGE_MSG", b"Merge branch 'x'\n# Conflicts:\n#\ta.txt\n")
        sha, summary = ops.commit(self.repo, [])
        self.assertEqual(self.repo.read_commit(sha).message, "Merge branch 'x'\n")
        self.assertIsNone(self.repo.read_state("MERGE_MSG"))

    def test_missing_identity(self):
        os.environ.pop("MINIGIT_AUTHOR_NAME")
        os.environ.pop("MINIGIT_AUTHOR_EMAIL")
        self.write("a.txt", "a\n")
        self.add("a.txt")
        with self.assertRaises(MinigitError) as cm:
            ops.commit(self.repo, ["x"])
        self.assertIn("please set your identity", cm.exception.message)
        self.assertIsNone(self.repo.head_commit())
        ops.config_cmd(self.repo, "user.name", "Cfg User", global_=True)
        ops.config_cmd(self.repo, "user.email", "cfg@example.com", global_=True)
        sha, _ = ops.commit(self.repo, ["x"])
        self.assertEqual(self.repo.read_commit(sha).author.email, "cfg@example.com")

    def test_conflict_refusal_and_merge_commit(self):
        base = self.commit_file("c.txt", "1\n")
        other = self.commit_file("o.txt", "o\n")
        with locked_index(self.repo) as index:
            index.put(IndexEntry("c.txt", "100644", hash_object("blob", b"o\n"), stage=2))
            index.put(IndexEntry("c.txt", "100644", hash_object("blob", b"t\n"), stage=3))
        self.repo.write_state("MERGE_HEAD", (base + "\n").encode())
        with self.assertRaises(MinigitError) as cm:
            ops.commit(self.repo, ["m"])
        self.assertEqual(cm.exception.message, "cannot commit: unresolved conflicts in: c.txt")
        self.write("c.txt", "resolved\n")
        self.add("c.txt")
        sha, _ = ops.commit(self.repo, ["merge"])
        self.assertEqual(self.repo.read_commit(sha).parents, (other, base))
        self.assertIsNone(self.repo.merge_head())

    def test_commit_all(self):
        self.commit_file("a.txt", "a\n")
        self.commit_file("b.txt", "b\n")
        self.write("a.txt", "a2\n")
        os.unlink(os.path.join(self.wt, "b.txt"))
        self.write("new.txt", "n\n")
        sha, _ = ops.commit(self.repo, ["all"], all_=True)
        flat = self.repo.head_flat()
        self.assertEqual(sorted(flat), ["a.txt"])
        self.assertEqual(flat["a.txt"][1], hash_object("blob", b"a2\n"))
        self.assertEqual(self.porcelain(), "?? new.txt\n")

    def test_detached_commit_summary(self):
        c1 = self.commit_file("a.txt", "a\n")
        ops.switch(self.repo, c1, detach=True)
        self.write("a.txt", "d\n")
        self.add("a.txt")
        sha, summary = ops.commit(self.repo, ["detached"])
        self.assertEqual(summary, f"[detached HEAD {sha[:7]}] detached")
        self.assertEqual(self.repo.refs.resolve("HEAD"), sha)
        self.assertEqual(self.repo.refs.resolve("refs/heads/main"), c1)

    # --- commit --amend -----------------------------------------------------
    def amend(self, *messages: str, **kw) -> tuple[str, str]:
        self.clock += 60
        os.environ["MINIGIT_AUTHOR_DATE"] = f"{self.clock} +0000"
        return ops.commit(self.repo, list(messages), amend=True, stat=True, **kw)

    def test_amend_replaces_head_keeping_parents_and_author(self):
        c1 = self.commit_file("a.txt", "a\n", "one")
        c2 = self.commit_file("b.txt", "b\n", "two")
        old = self.repo.read_commit(c2)
        self.write("c.txt", "c\n")
        self.add("c.txt")
        sha, summary = self.amend("two, fixed", "body")
        self.assertNotEqual(sha, c2)
        self.assertEqual(summary, f"[main {sha[:7]}] two, fixed\n 2 files changed, 2 insertions(+)")
        new = self.repo.read_commit(sha)
        self.assertEqual(new.parents, (c1,))
        self.assertEqual(new.message, "two, fixed\n\nbody\n")
        self.assertEqual(new.author, old.author)                       # name, email and date kept
        self.assertEqual(new.committer.timestamp, self.clock)          # the committer is new
        self.assertEqual(sorted(self.repo.head_flat()), ["a.txt", "b.txt", "c.txt"])
        self.assertEqual(self.repo.head_commit(), sha)
        self.assertEqual(self.repo.refs.read_special("ORIG_HEAD"), c2)
        self.assertEqual(self.porcelain(), "")

    def test_amend_without_message_keeps_it_and_needs_no_change(self):
        c1 = self.commit_file("a.txt", "a\n", "one")
        sha, summary = self.amend()      # nothing changed: allowed without --allow-empty, as in git
        self.assertNotEqual(sha, c1)
        self.assertEqual(self.repo.read_commit(sha).message, "one\n")
        self.assertEqual(self.repo.read_commit(sha).parents, ())
        self.assertEqual(summary, f"[main {sha[:7]}] one\n 1 file changed, 1 insertion(+)")
        self.assertEqual(self.repo.refs.read_special("ORIG_HEAD"), c1)

    def test_amend_with_all_restages_tracked_files(self):
        self.commit_file("a.txt", "a\n", "one")
        self.commit_file("b.txt", "b\n", "two")
        self.write("a.txt", "a2\n")
        os.unlink(os.path.join(self.wt, "b.txt"))
        self.write("new.txt", "n\n")
        sha, _ = self.amend(all_=True)
        flat = self.repo.head_flat()
        self.assertEqual(sorted(flat), ["a.txt"])
        self.assertEqual(flat["a.txt"][1], hash_object("blob", b"a2\n"))
        self.assertEqual(self.repo.read_commit(sha).message, "two\n")
        self.assertEqual(self.porcelain(), "?? new.txt\n")

    def test_amend_detached_head(self):
        c1 = self.commit_file("a.txt", "a\n", "one")
        c2 = self.commit_file("a.txt", "b\n", "two")
        ops.switch(self.repo, c2, detach=True)
        sha, summary = self.amend("two again")
        self.assertEqual(summary, f"[detached HEAD {sha[:7]}] two again\n 1 file changed, 1 insertion(+), 1 deletion(-)")
        self.assertEqual(self.repo.refs.resolve("HEAD"), sha)
        self.assertEqual(self.repo.refs.resolve("refs/heads/main"), c2)
        self.assertEqual(self.repo.read_commit(sha).parents, (c1,))

    def test_amend_refusals(self):
        with self.assertRaises(MinigitError) as cm:
            ops.commit(self.repo, ["x"], amend=True)
        self.assertEqual(cm.exception.message, "you have nothing to amend")
        c1 = self.commit_file("a.txt", "a\n", "one")
        c2 = self.commit_file("a.txt", "b\n", "two")
        for kind, state in (("merge", "MERGE_HEAD"), ("cherry-pick", "CHERRY_PICK_HEAD"), ("revert", "REVERT_HEAD")):
            self.repo.refs.write_special(state, c1)
            with self.assertRaises(MinigitError) as cm:
                ops.commit(self.repo, ["x"], amend=True)
            self.assertEqual(cm.exception.message, f"you cannot amend during a {kind}")
            self.repo.refs.write_special(state, None)
        os.makedirs(self.repo.gpath("rebase"))
        with self.assertRaises(MinigitError) as cm:
            ops.commit(self.repo, ["x"], amend=True)
        self.assertEqual(cm.exception.message, "you cannot amend during a rebase")
        os.rmdir(self.repo.gpath("rebase"))
        blob = hash_object("blob", b"x\n")
        with locked_index(self.repo) as index:
            index.remove("a.txt")
            for stage in (1, 2, 3):
                index.put(IndexEntry(path="a.txt", mode="100644", sha=blob, size=0, mtime_ns=0, stage=stage))
        with self.assertRaises(MinigitError) as cm:
            ops.commit(self.repo, ["x"], amend=True)
        self.assertEqual(cm.exception.message, "cannot commit: unresolved conflicts in: a.txt")
        self.assertEqual(self.repo.head_commit(), c2)   # nothing was written by any refusal
        self.assertIsNone(self.repo.refs.read_special("ORIG_HEAD"))


# =========================================================================== log / show

class HistoryTests(RepoCase):
    def _chain(self, n: int, parent: str | None = None, ts: int = 1700000000) -> list[str]:
        """``n`` commits of an empty tree written directly to the store, oldest first."""
        from minigit.objects import Commit, Signature, serialize_commit
        store = self.repo.objects
        tree = store.write("tree", b"")
        shas: list[str] = []
        for i in range(n):
            sig = Signature("T", "t@example.com", ts + i, 0)
            parents = (parent,) if parent else ()
            parent = store.write("commit", serialize_commit(
                Commit(tree=tree, parents=parents, author=sig, committer=sig, message=f"c{i}\n")))
            shas.append(parent)
        return shas

    def test_log_n_stops_walking_once_n_commits_are_taken(self):
        import time
        from unittest import mock
        shas = self._chain(300)
        self.repo.refs.update("refs/heads/main", shas[-1])
        reads: list[str] = []
        real = self.repo.objects.read_typed

        def counting(sha, kind):
            reads.append(sha)
            return real(sha, kind)

        with mock.patch.object(self.repo.objects, "read_typed", side_effect=counting):
            t0 = time.monotonic()
            entries = history.log_entries(self.repo, None, 1)
            elapsed = time.monotonic() - t0
        self.assertEqual([s for s, _ in entries], [shas[-1]])
        self.assertEqual(reads, [shas[-1]])     # one commit read, not 300
        self.assertLess(elapsed, 1.0)
        full = [s for s, _ in history.log_entries(self.repo, None, None)]
        self.assertEqual(full, shas[::-1])
        self.assertEqual([s for s, _ in history.log_entries(self.repo, None, 10)], full[:10])
        self.assertEqual([s for s, _ in history.log_entries(self.repo, None, 500)], full)

    def test_log_n_with_a_merge_matches_the_full_order(self):
        from minigit.objects import Commit, Signature, serialize_commit
        base = self._chain(3)
        left = self._chain(2, base[-1], ts=1700000100)
        right = self._chain(2, base[-1], ts=1700000050)
        store = self.repo.objects
        sig = Signature("T", "t@example.com", 1700000200, 0)
        merge = store.write("commit", serialize_commit(
            Commit(tree=store.write("tree", b""), parents=(left[-1], right[-1]), author=sig, committer=sig,
                   message="merge\n")))
        tip = self._chain(2, merge, ts=1700000300)[-1]
        self.repo.refs.update("refs/heads/main", tip)
        full = [s for s, _ in history.log_entries(self.repo, None, None)]
        self.assertEqual(len(full), 10)
        for n in range(1, 12):
            self.assertEqual([s for s, _ in history.log_entries(self.repo, None, n)], full[:n], n)

    def test_unborn(self):
        with self.assertRaises(MinigitError) as cm:
            history.log_entries(self.repo, None, None)
        self.assertEqual(cm.exception.message, "your current branch 'main' does not have any commits yet")

    def test_order_and_decorations(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        c2 = self.commit_file("a.txt", "2\n", "two")
        ops.branch_create(self.repo, "feature", c1)
        self.repo.refs.update("refs/remotes/origin/main", c2)
        entries = history.log_entries(self.repo, None, None)
        self.assertEqual([s for s, _ in entries], [c2, c1])
        self.assertEqual(history.log_entries(self.repo, None, 1)[0][0], c2)
        text = history.format_log(self.repo, entries, oneline=True)
        self.assertEqual(text, f"{c2[:7]} (HEAD -> main, origin/main) two\n{c1[:7]} (feature) one\n")
        full = history.format_log(self.repo, entries, oneline=False)
        self.assertTrue(full.startswith(f"commit {c2} (HEAD -> main, origin/main)\n"
                                        "Author: Test User <test@example.com>\n"
                                        "Date:   "))
        self.assertIn("\n\n    two\n\ncommit " + c1, full)
        self.assertTrue(full.endswith("    one\n"))
        # --all and explicit revs
        c3 = self.commit_file("b.txt", "3\n", "three")
        ops.switch(self.repo, "feature")
        c4 = self.commit_file("c.txt", "4\n", "four")
        self.assertEqual([s for s, _ in history.log_entries(self.repo, None, None)], [c4, c1])
        all_entries = history.log_entries(self.repo, None, None, all_refs=True)
        self.assertEqual(set(s for s, _ in all_entries), {c1, c2, c3, c4})
        self.assertEqual([s for s, _ in history.log_entries(self.repo, ["main"], None)], [c3, c2, c1])
        decor = history.decorations(self.repo)
        self.assertEqual(decor[c4], ["HEAD -> feature"])
        self.assertEqual(decor[c3], ["main"])
        ops.switch(self.repo, c1, detach=True)
        self.assertEqual(history.decorations(self.repo)[c1], ["HEAD"])
        self.assertIn(f"{c1[:7]} (HEAD) one", history.format_log(self.repo, history.log_entries(
            self.repo, None, None), oneline=True))

    def test_decorations_put_remote_tracking_refs_before_other_branches(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        ops.branch_create(self.repo, "extra")
        ops.branch_create(self.repo, "aaa")
        self.repo.refs.update("refs/remotes/origin/main", c1)
        self.assertEqual(history.decorations(self.repo)[c1], ["HEAD -> main", "origin/main", "aaa", "extra"])
        # a stray refs/heads/HEAD never shows up as a bare "HEAD"
        self.repo.refs.update("refs/heads/HEAD", c1)
        self.assertIn("heads/HEAD", history.decorations(self.repo)[c1])
        self.assertNotIn("HEAD", history.decorations(self.repo)[c1])

    def test_show_merge_commit_shows_the_resolution(self):
        from minigit import merge as mergemod
        self.commit_file("f.txt", "base\n", "base")
        self.commit_file("g.txt", "g\n", "g")
        ops.switch(self.repo, None, create="side")
        self.commit_file("f.txt", "side\n", "side")
        self.commit_file("s.txt", "s\n", "side only")
        ops.switch(self.repo, "main")
        self.commit_file("f.txt", "main\n", "main")
        self.assertEqual(mergemod.merge(self.repo, "side").kind, "conflict")
        self.write("f.txt", "resolved\n")
        self.add("f.txt")
        m = self.commit("merge")
        first = self.repo.read_commit(m).parents[0]
        out = history.show(self.repo, m).decode()
        self.assertIn("Merge: ", out)
        self.assertIn(f"\n    merge\n\n(merge commit: showing the diff against the first parent {first[:7]})\n"
                      "\ndiff --git a/f.txt b/f.txt\n", out)
        self.assertIn("-main\n+resolved\n", out)
        self.assertIn("diff --git a/s.txt b/s.txt\n", out)  # new since the first parent

    def _merge_history(self) -> tuple[str, str, str, str]:
        """``c1 <- c2`` on main, ``c1 <- s1`` on side, merged into main: ``(c1, c2, s1, m)``."""
        c1 = self.commit_file("a.txt", "1\n", "one")
        ops.branch_create(self.repo, "side", c1)
        c2 = self.commit_file("a.txt", "2\n", "two")
        ops.switch(self.repo, "side")
        s1 = self.commit_file("s.txt", "s\n", "s1")
        ops.switch(self.repo, "main")
        m = self.merge("side", "merge")
        self.assertEqual(self.repo.read_commit(m).parents, (c2, s1))
        return c1, c2, s1, m

    def test_show_merge_commit_diffs_against_the_first_parent(self):
        c1, c2, s1, m = self._merge_history()
        out = history.show(self.repo, m).decode()
        self.assertTrue(out.startswith(f"commit {m} (HEAD -> main)\nMerge: {c2[:7]} {s1[:7]}\n"))
        self.assertIn(f"\n    merge\n\n(merge commit: showing the diff against the first parent {c2[:7]})\n\n"
                      "diff --git a/s.txt b/s.txt\nnew file mode 100644\n", out)
        self.assertIn("+s\n", out)
        self.assertNotIn("a.txt", out)  # unchanged against the first parent
        stat = history.show(self.repo, m, stat=True).decode()
        self.assertTrue(stat.endswith(
            f"(merge commit: showing the diff against the first parent {c2[:7]})\n\n"
            " s.txt | 1 +\n 1 file changed, 1 insertion(+)\n"), stat)
        # -m: the diff against each parent in turn, each under the full header
        each = history.show(self.repo, m, each_parent=True).decode()
        first, second = each.split(f"\n\ncommit {m} (from {s1}) (HEAD -> main)\n")
        self.assertTrue(first.startswith(f"commit {m} (from {c2}) (HEAD -> main)\nMerge: {c2[:7]} {s1[:7]}\n"))
        self.assertIn("\n    merge\n\ndiff --git a/s.txt b/s.txt\n", first)
        self.assertNotIn("a.txt", first)
        self.assertTrue(second.startswith(f"Merge: {c2[:7]} {s1[:7]}\nAuthor: "))
        self.assertIn("\n    merge\n\ndiff --git a/a.txt b/a.txt\n", second)
        self.assertIn("-1\n+2\n", second)
        self.assertNotIn("s.txt", second)
        self.assertNotIn("(merge commit:", each)
        # -m and --stat on an ordinary commit change nothing but the body
        self.assertEqual(history.show(self.repo, c2, each_parent=True), history.show(self.repo, c2))
        self.assertTrue(history.show(self.repo, c2, stat=True).decode().endswith(
            "\n    two\n\n a.txt | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"))

    def test_log_paths(self):
        def shas(entries):
            return [s for s, _ in entries]

        c1 = self.commit_file("a.txt", "1\n", "one")
        c2 = self.commit_file("d/x.txt", "x\n", "two")
        c3 = self.commit_file("a.txt", "2\n", "three")
        c4 = self.commit_file("d/y.txt", "y\n", "four")
        log = lambda paths, **kw: shas(history.log_entries(self.repo, None, kw.pop("n", None), paths=paths, **kw))
        self.assertEqual(log(["a.txt"]), [c3, c1])
        self.assertEqual(log(["d"]), [c4, c2])            # a directory matches everything under it
        self.assertEqual(log(["d/"]), [c4, c2])
        self.assertEqual(log(["d/x.txt"]), [c2])
        self.assertEqual(log(["a.txt"], n=1), [c3])
        self.assertEqual(log(["nope"]), [])
        self.assertEqual(log(["a.txt", "d/y.txt"]), [c4, c3, c1])
        self.assertEqual(log([""]), [c4, c3, c2, c1])     # the root: every commit that changes anything
        self.assertEqual(log(["a.txt/x"]), [])            # a file is not a directory
        # a merge counts when its tree differs from its *first* parent's tree at the path
        ops.branch_create(self.repo, "side", c1)
        ops.switch(self.repo, "side")
        s1 = self.commit_file("s.txt", "s\n", "side")
        ops.switch(self.repo, "main")
        m = self.merge("side", "merge")
        self.assertEqual(log(["s.txt"]), [m, s1])
        self.assertEqual(log(["a.txt"]), [c3, c1])
        self.assertEqual(shas(history.log_entries(self.repo, ["side"], None, paths=["s.txt"])), [s1])
        self.assertEqual(shas(history.log_entries(self.repo, None, None, all_refs=True, paths=["a.txt"])), [c3, c1])

    def _graph(self, oneline: bool, **kw) -> str:
        entries = history.log_entries(self.repo, None, None, **{k: v for k, v in kw.items() if k == "paths"})
        return history.format_log(self.repo, entries, oneline, graph=True, **kw)

    def test_graph_linear(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        c2 = self.commit_file("a.txt", "2\n", "two")
        c3 = self.commit_file("a.txt", "3\n", "three")
        self.assertEqual(self._graph(True),
                         f"* {c3[:7]} (HEAD -> main) three\n* {c2[:7]} two\n* {c1[:7]} one\n")
        author = "Author: Test User <test@example.com>"
        self.assertEqual(self._graph(False), (
            f"* commit {c3} (HEAD -> main)\n| {author}\n| Date:   {self.date(c3)}\n| \n|     three\n| \n"
            f"* commit {c2}\n| {author}\n| Date:   {self.date(c2)}\n| \n|     two\n| \n"
            f"* commit {c1}\n  {author}\n  Date:   {self.date(c1)}\n  \n      one\n"))

    def test_graph_one_merge(self):
        c1, c2, s1, m = self._merge_history()
        self.assertEqual(self._graph(True), (
            f"*   {m[:7]} (HEAD -> main) merge\n"
            "|\\  \n"
            f"| * {s1[:7]} (side) s1\n"
            f"* | {c2[:7]} two\n"
            "|/  \n"
            f"* {c1[:7]} one\n"))
        author = "Author: Test User <test@example.com>"
        self.assertEqual(self._graph(False), (
            f"*   commit {m} (HEAD -> main)\n"
            f"|\\  Merge: {c2[:7]} {s1[:7]}\n"
            f"| | {author}\n| | Date:   {self.date(m)}\n| | \n| |     merge\n| | \n"
            f"| * commit {s1} (side)\n"
            f"| | {author}\n| | Date:   {self.date(s1)}\n| | \n| |     s1\n| | \n"
            f"* | commit {c2}\n"
            f"|/  {author}\n|   Date:   {self.date(c2)}\n|   \n|       two\n| \n"
            f"* commit {c1}\n"
            f"  {author}\n  Date:   {self.date(c1)}\n  \n      one\n"))
        # -n keeps the lanes leaving the picture, as git does
        one = history.format_log(self.repo, history.log_entries(self.repo, None, 1), True, graph=True)
        self.assertEqual(one, f"*   {m[:7]} (HEAD -> main) merge\n|\\  \n")

    def test_graph_two_branches_merged_in_turn(self):
        c1 = self.commit_file("c.txt", "1\n", "c1")
        ops.branch_create(self.repo, "a", c1)
        ops.branch_create(self.repo, "b", c1)
        ops.switch(self.repo, "a")
        a1 = self.commit_file("a.txt", "a\n", "a1")
        ops.switch(self.repo, "b")
        b1 = self.commit_file("b.txt", "b\n", "b1")
        ops.switch(self.repo, "main")
        ma = self.merge("a", "MA", no_ff=True)
        mb = self.merge("b", "MB", no_ff=True)
        self.assertEqual(self._graph(True), (
            f"*   {mb[:7]} (HEAD -> main) MB\n"
            "|\\  \n"
            f"* \\   {ma[:7]} MA\n"
            "|\\ \\  \n"
            f"| | * {b1[:7]} (b) b1\n"
            "| |/  \n"
            "|/|   \n"
            f"| * {a1[:7]} (a) a1\n"
            "|/  \n"
            f"* {c1[:7]} c1\n"))
        # with a path filter the graph joins the shown commits
        self.assertEqual(self._graph(True, paths=["b.txt"]), (
            f"* {mb[:7]} (HEAD -> main) MB\n"
            f"* {b1[:7]} (b) b1\n"))

    def test_log_stat(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        c2 = self.commit_file("a.txt", "1\n2\n", "two")
        text = history.format_log(self.repo, history.log_entries(self.repo, None, None), False, stat=True)
        self.assertIn(f"\n    two\n\n a.txt | 1 +\n 1 file changed, 1 insertion(+)\n\ncommit {c1}\n", text)
        self.assertTrue(text.endswith("\n    one\n\n a.txt | 1 +\n 1 file changed, 1 insertion(+)\n"))
        # a merge gets no stat block (git shows no diff for merges in log); --graph prefixes the block
        ops.branch_create(self.repo, "side", c1)
        ops.switch(self.repo, "side")
        self.commit_file("s.txt", "s\n", "s1")
        ops.switch(self.repo, "main")
        m = self.merge("side", "merge")
        text = history.format_log(self.repo, history.log_entries(self.repo, None, None), False, stat=True, graph=True)
        self.assertIn(f"*   commit {m} (HEAD -> main)\n", text)
        self.assertIn("| |     merge\n| | \n| * commit ", text)
        self.assertIn("| |     s1\n| | \n| |  s.txt | 1 +\n| |  1 file changed, 1 insertion(+)\n| | \n", text)

    def test_topo_order_beats_timestamps(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        ops.branch_create(self.repo, "side", c1)
        c2 = self.commit_file("a.txt", "2\n", "two")
        ops.switch(self.repo, "side")
        self.clock -= 600  # side commit is "older" than c2 but must still print before its parent
        c3 = self.commit_file("b.txt", "3\n", "three")
        tree = self.repo.read_commit(c3).tree
        self.clock += 1000
        os.environ["MINIGIT_AUTHOR_DATE"] = f"{self.clock} +0000"
        m = ops.create_commit(self.repo, tree, [c3, c2], "merge\n")
        order = [s for s, _ in history.log_entries(self.repo, [m], None)]
        self.assertEqual(order[0], m)
        self.assertEqual(order[-1], c1)
        self.assertLess(order.index(c2), order.index(c3))  # newer first among siblings
        full = history.format_log(self.repo, history.log_entries(self.repo, [m], 1), oneline=False)
        self.assertIn(f"commit {m}\nMerge: {c3[:7]} {c2[:7]}\n", full)

    def test_show(self):
        c1 = self.commit_file("a.txt", "a\n", "one")
        c2 = self.commit_file("a.txt", "b\n", "two")
        out = history.show(self.repo, c2).decode()
        self.assertTrue(out.startswith(f"commit {c2} (HEAD -> main)\n"))
        self.assertIn("\n    two\n\ndiff --git a/a.txt b/a.txt\n", out)
        self.assertIn("@@ -1 +1 @@\n-a\n+b\n", out)
        root = history.show(self.repo, c1).decode()
        self.assertIn("new file mode 100644", root)
        self.assertEqual(history.show(self.repo, "HEAD:a.txt"), b"b\n")
        tree = history.show(self.repo, "HEAD:").decode()
        self.assertEqual(tree, f"100644 blob {hash_object('blob', b'b' + chr(10).encode())}\ta.txt\n")
        self.write("d/x.txt", "x\n")
        self.add("d")
        self.commit("three")
        self.assertIn("040000 tree ", history.show(self.repo, "HEAD:").decode())
        with self.assertRaises(RevisionError):
            history.show(self.repo, "nope")


# =========================================================================== branch

class BranchTests(RepoCase):
    def test_list_with_upstreams(self):
        # -vv: [origin/main], ahead, behind, both, gone, and no column without an upstream
        c1 = self.commit_file("a.txt", "1\n", "one")
        c2 = self.commit_file("a.txt", "2\n", "two")
        ops.branch_create(self.repo, "feature", c1)
        ops.branch_create(self.repo, "plain", c1)
        ops.set_upstream(self.repo, "main", "origin", "refs/heads/main")
        ops.set_upstream(self.repo, "feature", "origin", "refs/heads/feature")
        self.repo.refs.update("refs/remotes/origin/main", c2)
        self.repo.refs.update("refs/remotes/origin/feature", c1)

        def listing():
            return ops.format_branch_list(ops.branch_list(self.repo, upstreams=True), True)

        self.assertEqual(listing(), f"  feature {c1[:7]} [origin/feature] one\n"
                                    f"* main    {c2[:7]} [origin/main] two\n"
                                    f"  plain   {c1[:7]} one\n")
        # -v without upstreams prints no column at all
        self.assertEqual(ops.format_branch_list(ops.branch_list(self.repo), True),
                         f"  feature {c1[:7]} one\n* main    {c2[:7]} two\n  plain   {c1[:7]} one\n")
        self.repo.refs.update("refs/remotes/origin/main", c1)
        self.assertIn(f"* main    {c2[:7]} [origin/main: ahead 1] two\n", listing())
        self.repo.refs.update("refs/remotes/origin/feature", c2)
        self.assertIn(f"  feature {c1[:7]} [origin/feature: behind 1] one\n", listing())
        ops.switch(self.repo, "feature")
        c3 = self.commit_file("b.txt", "3\n", "three")
        self.assertIn(f"* feature {c3[:7]} [origin/feature: ahead 1, behind 1] three\n", listing())
        items = ops.branch_list(self.repo, upstreams=True)
        feature = next(i for i in items if i.name == "feature")
        self.assertEqual((feature.upstream, feature.ahead, feature.behind, feature.gone), ("origin/feature", 1, 1, False))
        self.assertIsNone(next(i for i in items if i.name == "plain").upstream)
        # the tracking ref disappeared (fetch pruned it): gone
        self.repo.refs.update("refs/remotes/origin/feature", None)
        self.assertIn(f"* feature {c3[:7]} [origin/feature: gone] three\n", listing())
        gone = next(i for i in ops.branch_list(self.repo, upstreams=True) if i.name == "feature")
        self.assertTrue(gone.gone)
        self.assertEqual(gone.tracking(), "[origin/feature: gone]")
        # a local upstream (remote ".") shows the branch name
        ops.set_upstream(self.repo, "plain", ".", "refs/heads/main")
        self.assertIn(f"  plain   {c1[:7]} [main: behind 1] one\n", listing())
        # remote branches never get the column, and the detached HEAD line neither
        ops.switch(self.repo, c3, detach=True)
        text = ops.format_branch_list(ops.branch_list(self.repo, all_=True, upstreams=True), True)
        self.assertTrue(text.startswith(f"* (HEAD detached at {c3[:7]}) {c3[:7]} three\n"), text)
        self.assertIn(f"  remotes/origin/main        {c1[:7]} one\n", text)

    def test_set_and_unset_upstream(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        ops.branch_create(self.repo, "feature")
        with self.assertRaises(MinigitError) as cm:
            ops.set_upstream_to(self.repo, "origin/main")
        self.assertEqual(cm.exception.message, "the requested upstream branch 'origin/main' does not exist")
        self.assertIsNone(ops.get_upstream(self.repo, "main"))
        self.repo.refs.update("refs/remotes/origin/main", c1)
        self.assertEqual(ops.set_upstream_to(self.repo, "origin/main"), "branch 'main' set up to track 'origin/main'.")
        self.assertEqual(ops.get_upstream(self.repo, "main"), ("origin", "refs/heads/main"))
        # an explicit branch, the full ref spelling, and a remote whose name contains a slash
        self.assertEqual(ops.set_upstream_to(self.repo, "refs/remotes/origin/main", "feature"),
                         "branch 'feature' set up to track 'origin/main'.")
        self.assertEqual(ops.get_upstream(self.repo, "feature"), ("origin", "refs/heads/main"))
        self.repo.config.set("remote.up/stream.url", "http://h:1/p")
        self.repo.refs.update("refs/remotes/up/stream/dev", c1)
        ops.set_upstream_to(self.repo, "up/stream/dev", "feature")
        self.assertEqual(ops.get_upstream(self.repo, "feature"), ("up/stream", "refs/heads/dev"))
        # a local branch as upstream
        self.assertEqual(ops.set_upstream_to(self.repo, "main", "feature"), "branch 'feature' set up to track 'main'.")
        self.assertEqual(ops.get_upstream(self.repo, "feature"), (".", "refs/heads/main"))
        with self.assertRaises(MinigitError) as cm:
            ops.set_upstream_to(self.repo, "origin/main", "nosuch")
        self.assertEqual(cm.exception.message, "branch 'nosuch' not found")
        # unset: the keys and the empty section go away; a second time is an error
        ops.unset_upstream(self.repo)
        self.assertIsNone(ops.get_upstream(self.repo, "main"))
        self.assertNotIn("[branch \"main\"]", self.read(".minigit/config").decode())
        with self.assertRaises(MinigitError) as cm:
            ops.unset_upstream(self.repo, "main")
        self.assertEqual(cm.exception.message, "branch 'main' has no upstream information")
        ops.unset_upstream(self.repo, "feature")
        self.assertIsNone(ops.get_upstream(self.repo, "feature"))
        # detached HEAD: no default branch
        ops.switch(self.repo, c1, detach=True)
        for call in (lambda: ops.unset_upstream(self.repo), lambda: ops.set_upstream_to(self.repo, "origin/main")):
            with self.assertRaises(MinigitError) as cm:
                call()
            self.assertEqual(cm.exception.message, "HEAD does not point to a branch")

    def test_list_create_delete(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        self.assertEqual(ops.branch_create(self.repo, "feature"), c1)
        with self.assertRaises(MinigitError) as cm:
            ops.branch_create(self.repo, "feature")
        self.assertEqual(cm.exception.message, "a branch named 'feature' already exists")
        with self.assertRaises(MinigitError):
            ops.branch_create(self.repo, "bad name")
        self.repo.refs.update("refs/remotes/origin/main", c1)
        items = ops.branch_list(self.repo)
        self.assertEqual([(i.name, i.current) for i in items], [("feature", False), ("main", True)])
        self.assertEqual(ops.format_branch_list(items, False), "  feature\n* main\n")
        self.assertEqual(ops.format_branch_list(items, True), f"  feature {c1[:7]} one\n* main    {c1[:7]} one\n")
        self.assertEqual([i.name for i in ops.branch_list(self.repo, remotes=True)], ["origin/main"])
        self.assertEqual([i.name for i in ops.branch_list(self.repo, all_=True)],
                         ["feature", "main", "remotes/origin/main"])
        self.assertEqual(ops.format_branch_list(ops.branch_list(self.repo, all_=True), False),
                         "  feature\n* main\n  remotes/origin/main\n")
        self.assertEqual(ops.branch_delete(self.repo, "feature"), f"Deleted branch feature (was {c1[:7]}).")
        with self.assertRaises(MinigitError) as cm:
            ops.branch_delete(self.repo, "feature")
        self.assertEqual(cm.exception.message, "branch 'feature' not found")

    def test_delete_safety(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        with self.assertRaises(MinigitError) as cm:
            ops.branch_delete(self.repo, "main")
        self.assertEqual(cm.exception.message, "cannot delete the branch 'main' you are on")
        ops.switch(self.repo, None, create="feature")
        c2 = self.commit_file("b.txt", "2\n", "two")
        ops.switch(self.repo, "main")
        with self.assertRaises(MinigitError) as cm:
            ops.branch_delete(self.repo, "feature")
        self.assertEqual(cm.exception.message, "the branch 'feature' is not fully merged (use -D)")
        # merged into the upstream tracking ref counts as merged
        self.repo.refs.update("refs/remotes/origin/feature", c2)
        ops.set_upstream(self.repo, "feature", "origin", "refs/heads/feature")
        self.assertEqual(ops.get_upstream(self.repo, "feature"), ("origin", "refs/heads/feature"))
        ops.branch_delete(self.repo, "feature")
        self.assertIsNone(ops.get_upstream(self.repo, "feature"))
        ops.branch_create(self.repo, "feature2", c2)
        with self.assertRaises(MinigitError):
            ops.branch_delete(self.repo, "feature2")
        ops.branch_delete(self.repo, "feature2", force=True)
        self.assertIsNone(self.repo.refs.resolve("refs/heads/feature2"))
        self.assertEqual(self.repo.refs.resolve("refs/heads/main"), c1)

    def test_invalid_branch_names_are_refused(self):
        self.commit_file("a.txt", "1\n", "one")
        for name in ("HEAD", "@", "-x", "a..b", "x.lock", "end.", "a//b", "a b", "refs/heads/x", "q?"):
            with self.subTest(name=name):
                with self.assertRaises(MinigitError) as cm:
                    ops.branch_create(self.repo, name)
                self.assertEqual(cm.exception.message, f"'{name}' is not a valid branch name")
                with self.assertRaises(MinigitError):
                    ops.switch(self.repo, None, create=name)
        self.assertEqual(list(self.repo.refs.list("refs/heads/")), ["refs/heads/main"])

    def test_too_long_branch_name_is_a_clean_error(self):
        # QA bug 7: `branch <256 chars>` was "fatal: internal error: [Errno 22]"
        self.commit_file("a.txt", "1\n", "one")
        ops.branch_create(self.repo, "b" * 250)
        for name in ("b" * 256, "c" * 300, "dir/" + "e" * 251):
            with self.subTest(length=len(name)):
                with self.assertRaises(MinigitError) as cm:
                    ops.branch_create(self.repo, name)
                self.assertEqual(cm.exception.message, f"'{name}' is not a valid branch name: too long "
                                                       "(a component is over 250 bytes)")
                with self.assertRaises(MinigitError):
                    ops.switch(self.repo, name)
        self.assertEqual(list(self.repo.refs.list("refs/heads/")), ["refs/heads/" + "b" * 250, "refs/heads/main"])

    def test_list_marks_a_detached_head(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        self.commit_file("a.txt", "2\n", "two")
        ops.branch_create(self.repo, "topic")
        ops.switch(self.repo, c1, detach=True)
        items = ops.branch_list(self.repo)
        self.assertEqual(ops.format_branch_list(items, False),
                         f"* (HEAD detached at {c1[:7]})\n  main\n  topic\n")
        self.assertTrue(ops.format_branch_list(items, True).startswith(
            f"* (HEAD detached at {c1[:7]}) {c1[:7]} one\n"))
        self.assertEqual([i.name for i in ops.branch_list(self.repo, remotes=True)], [])

    def test_create_on_unborn(self):
        with self.assertRaises(MinigitError):
            ops.branch_create(self.repo, "x")
        self.assertEqual(ops.switch(self.repo, None, create="dev"), "Switched to a new branch 'dev'")
        self.assertEqual(self.repo.refs.head_target(), "refs/heads/dev")
        sha = self.commit_file("a.txt", "a\n")
        self.assertEqual(self.repo.refs.resolve("refs/heads/dev"), sha)
        self.assertIsNone(self.repo.refs.resolve("refs/heads/main"))


# =========================================================================== switch

class SwitchTests(RepoCase):
    def test_switch_and_checkout_tree(self):
        c1 = self.commit_file("a.txt", "1\n")
        self.assertEqual(ops.switch(self.repo, None, create="feature"), "Switched to a new branch 'feature'")
        self.commit_file("only_feature.txt", "f\n")
        self.commit_file("a.txt", "feature\n")
        self.assertEqual(ops.switch(self.repo, "main"), "Switched to branch 'main'")
        self.assertEqual(self.read("a.txt"), b"1\n")
        self.assertFalse(self.exists("only_feature.txt"))
        self.assertEqual(self.porcelain(), "")
        self.assertEqual(ops.switch(self.repo, "main"), "Already on 'main'")
        self.assertEqual(ops.switch(self.repo, "feature"), "Switched to branch 'feature'")
        self.assertEqual(self.read("a.txt"), b"feature\n")
        self.assertTrue(self.exists("only_feature.txt"))
        self.assertEqual(self.repo.head_commit(), self.repo.refs.resolve("refs/heads/feature"))
        self.assertNotEqual(self.repo.head_commit(), c1)

    def test_refuses_to_clobber(self):
        self.commit_file("a.txt", "1\n")
        ops.switch(self.repo, None, create="feature")
        self.commit_file("a.txt", "2\n")
        self.write("a.txt", "dirty\n")
        with self.assertRaises(CheckoutConflictError) as cm:
            ops.switch(self.repo, "main")
        self.assertEqual(cm.exception.paths, ["a.txt"])
        self.assertIn("would be overwritten by checkout", cm.exception.message)
        self.assertEqual(self.read("a.txt"), b"dirty\n")
        self.assertEqual(self.repo.refs.current_branch(), "feature")
        # a non-conflicting untracked file survives the switch
        self.write("a.txt", "2\n")
        self.write("keep.txt", "k\n")
        ops.switch(self.repo, "main")
        self.assertTrue(self.exists("keep.txt"))

    def test_untracked_file_in_the_way_is_not_called_a_local_change(self):
        self.commit_file("a.txt", "1\n")
        ops.switch(self.repo, None, create="b2")
        self.commit_file("t.txt", "theirs\n")
        ops.switch(self.repo, "main")
        self.write("t.txt", "mine\n")
        with self.assertRaises(CheckoutConflictError) as cm:
            ops.switch(self.repo, "b2")
        self.assertEqual(cm.exception.paths, ["t.txt"])
        self.assertEqual(cm.exception.message,
                         "The following untracked working tree files would be overwritten by checkout:\n"
                         "\tt.txt\nPlease move or remove them before you switch branches.")
        self.assertEqual(self.read("t.txt"), b"mine\n")
        self.assertEqual(self.repo.refs.current_branch(), "main")

    def test_detach_and_checkout_rev(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        c2 = self.commit_file("a.txt", "2\n", "two")
        self.assertEqual(ops.switch(self.repo, c1, detach=True), f"HEAD is now at {c1[:7]} one")
        self.assertIsNone(self.repo.refs.current_branch())
        self.assertEqual(self.read("a.txt"), b"1\n")
        self.assertEqual(ops.switch(self.repo, "main"), "Switched to branch 'main'")
        self.assertEqual(self.read("a.txt"), b"2\n")
        # checkout <sha-prefix> detaches too (the CLI passes detach=True for a non-branch rev)
        self.assertEqual(ops.switch(self.repo, c1[:8], detach=True), f"HEAD is now at {c1[:7]} one")
        self.assertEqual(self.repo.refs.read_raw("HEAD"), c1)
        ops.switch(self.repo, "main")
        with self.assertRaises(MinigitError) as cm:
            ops.switch(self.repo, "nonexistent")
        self.assertEqual(cm.exception.message, "invalid reference: nonexistent")
        self.assertEqual(c2, self.repo.refs.resolve("refs/heads/main"))

    def test_switch_to_a_non_branch_requires_detach(self):
        c1 = self.commit_file("a.txt", "1\n", "one")
        self.commit_file("a.txt", "2\n", "two")
        self.repo.refs.update("refs/remotes/origin/main", c1)
        for target in (c1, c1[:7], "HEAD~1", "HEAD", "origin/main"):
            with self.subTest(target=target):
                with self.assertRaises(MinigitError) as cm:
                    ops.switch(self.repo, target)
                self.assertIn("a branch is expected, got", cm.exception.message)
                self.assertIn("--detach", cm.exception.hint)
                self.assertEqual(self.repo.refs.read_raw("HEAD"), "ref: refs/heads/main")
                self.assertEqual(self.read("a.txt"), b"2\n")
        with self.assertRaises(MinigitError) as cm:
            ops.switch(self.repo, "origin/main")
        self.assertEqual(cm.exception.message, "a branch is expected, got remote branch 'origin/main'")

    def test_dwim_remote_branch(self):
        c1 = self.commit_file("a.txt", "1\n")
        self.repo.refs.update("refs/remotes/origin/topic", c1)
        msg = ops.switch(self.repo, "topic")
        self.assertIn("Switched to a new branch 'topic'", msg)
        self.assertEqual(self.repo.refs.current_branch(), "topic")
        self.assertEqual(ops.get_upstream(self.repo, "topic"), ("origin", "refs/heads/topic"))
        # a second remote with the same name makes it ambiguous
        self.repo.refs.update("refs/remotes/other/dup", c1)
        self.repo.refs.update("refs/remotes/origin/dup", c1)
        with self.assertRaises(MinigitError):
            ops.switch(self.repo, "dup")

    def test_dwim_remote_branch_with_slash(self):
        # QA bug 6: `switch feature/x` creates a tracking branch like git does
        c1 = self.commit_file("a.txt", "1\n")
        self.repo.refs.update("refs/remotes/origin/feature/x", c1)
        msg = ops.switch(self.repo, "feature/x")
        self.assertEqual(msg, "branch 'feature/x' set up to track 'origin/feature/x'.\n"
                              "Switched to a new branch 'feature/x'")
        self.assertEqual(self.repo.refs.current_branch(), "feature/x")
        self.assertEqual(self.repo.refs.resolve("refs/heads/feature/x"), c1)
        self.assertEqual(ops.get_upstream(self.repo, "feature/x"), ("origin", "refs/heads/feature/x"))
        self.assertEqual(ops.switch(self.repo, "feature/x"), "Already on 'feature/x'")
        # two remotes have it: ambiguous, nothing created
        self.repo.refs.update("refs/remotes/origin/team/y", c1)
        self.repo.refs.update("refs/remotes/up/team/y", c1)
        with self.assertRaises(MinigitError) as cm:
            ops.switch(self.repo, "team/y")
        self.assertEqual(cm.exception.message, "'team/y' matched multiple (2) remote tracking branches")
        self.assertIsNone(self.repo.refs.read_raw("refs/heads/team/y"))
        # a remote-tracking name itself is still "a branch is expected", even if another remote
        # happens to have a branch called "origin/feature/x"
        self.repo.refs.update("refs/remotes/up/origin/feature/x", c1)
        with self.assertRaises(MinigitError) as cm:
            ops.switch(self.repo, "origin/feature/x")
        self.assertIn("a branch is expected", cm.exception.message)
        self.assertIsNone(self.repo.refs.read_raw("refs/heads/origin/feature/x"))

    def test_create_with_start_and_merge_guard(self):
        c1 = self.commit_file("a.txt", "1\n")
        c2 = self.commit_file("a.txt", "2\n")
        msg = ops.switch(self.repo, None, create="old", start=c1)
        self.assertEqual(msg, "Switched to a new branch 'old'")
        self.assertEqual(self.read("a.txt"), b"1\n")
        self.assertEqual(self.repo.refs.resolve("refs/heads/old"), c1)
        self.repo.refs.update("refs/remotes/origin/main", c2)
        msg = ops.switch(self.repo, None, create="tracked", start="origin/main")
        self.assertIn("set up to track 'origin/main'", msg)
        self.assertEqual(ops.get_upstream(self.repo, "tracked"), ("origin", "refs/heads/main"))
        self.repo.write_state("MERGE_HEAD", (c1 + "\n").encode())
        with self.assertRaises(MinigitError) as cm:
            ops.switch(self.repo, "main")
        self.assertEqual(cm.exception.message, "merge in progress; commit or run 'minigit merge --abort'")


# =========================================================================== reset / restore

class ResetRestoreTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.c1 = self.commit_file("a.txt", "1\n", "one")
        self.c2 = self.commit_file("a.txt", "2\n", "two")
        self.write("b.txt", "b\n")
        self.c3 = self.commit_file("b.txt", "b\n", "three")

    def test_soft(self):
        self.assertEqual(ops.reset(self.repo, "HEAD~1", "soft"), "")
        self.assertEqual(self.repo.head_commit(), self.c2)
        self.assertEqual(self.repo.refs.read_special("ORIG_HEAD"), self.c3)
        self.assertEqual(self.porcelain(), "A  b.txt\n")
        self.assertEqual(self.read("b.txt"), b"b\n")
        self.repo.write_state("MERGE_HEAD", (self.c1 + "\n").encode())
        with self.assertRaises(MinigitError):
            ops.reset(self.repo, "HEAD", "soft")

    def test_mixed(self):
        self.write("a.txt", "dirty\n")
        self.add("a.txt")
        self.repo.write_state("MERGE_HEAD", (self.c1 + "\n").encode())
        self.repo.write_state("MERGE_MSG", b"m\n")
        self.assertEqual(ops.reset(self.repo, "HEAD~2"), "")
        self.assertEqual(self.repo.head_commit(), self.c1)
        self.assertIsNone(self.repo.merge_head())
        self.assertIsNone(self.repo.read_state("MERGE_MSG"))
        self.assertEqual(read_index(self.repo).flat(), self.repo.head_flat())
        self.assertEqual(self.read("a.txt"), b"dirty\n")
        self.assertEqual(self.porcelain(), " M a.txt\n?? b.txt\n")
        self.assertEqual(read_index(self.repo).get("a.txt").mtime_ns, 0)

    def test_hard_keeps_untracked(self):
        self.write("a.txt", "dirty\n")
        self.write("untracked.txt", "u\n")
        self.write("staged_new.txt", "s\n")
        self.add("staged_new.txt")
        msg = ops.reset(self.repo, self.c1, "hard")
        self.assertEqual(msg, f"HEAD is now at {self.c1[:7]} one")
        self.assertEqual(self.read("a.txt"), b"1\n")
        self.assertFalse(self.exists("b.txt"))
        self.assertFalse(self.exists("staged_new.txt"))
        self.assertTrue(self.exists("untracked.txt"))
        self.assertEqual(self.porcelain(), "?? untracked.txt\n")
        self.assertEqual(self.repo.refs.read_special("ORIG_HEAD"), self.c3)
        # going forward again restores b.txt
        ops.reset(self.repo, "ORIG_HEAD", "hard")
        self.assertEqual(self.read("b.txt"), b"b\n")
        self.assertEqual(self.porcelain(), "?? untracked.txt\n")

    def test_hard_rebuilds_corrupt_index(self):
        with open(self.repo.gpath("index"), "wb") as f:
            f.write(b"garbage")
        with self.assertRaises(MinigitError):
            read_index(self.repo)
        ops.reset(self.repo, "HEAD", "hard")
        self.assertEqual(self.porcelain(), "")

    def test_reset_paths(self):
        self.write("a.txt", "x\n")
        self.add("a.txt")
        self.assertEqual(ops.reset_paths(self.repo, "HEAD", ["a.txt"], self.wt), ["a.txt"])
        self.assertEqual(self.porcelain(), " M a.txt\n")
        self.assertEqual(self.repo.head_commit(), self.c3)
        self.assertEqual(ops.reset_paths(self.repo, "HEAD~1", ["b.txt"], self.wt), ["b.txt"])
        self.assertEqual(self.porcelain(), " M a.txt\n?? b.txt\nD  b.txt\n")
        with self.assertRaises(MinigitError):
            ops.reset_paths(self.repo, "HEAD", ["zzz"], self.wt)

    def test_restore_worktree_from_index(self):
        self.write("a.txt", "dirty\n")
        self.assertEqual(ops.restore(self.repo, ["a.txt"], self.wt), ["a.txt"])
        self.assertEqual(self.read("a.txt"), b"2\n")
        self.assertEqual(self.porcelain(), "")
        os.unlink(os.path.join(self.wt, "b.txt"))
        ops.restore(self.repo, ["."], self.wt)
        self.assertEqual(self.read("b.txt"), b"b\n")
        with self.assertRaises(MinigitError) as cm:
            ops.restore(self.repo, ["nope.txt"], self.wt)
        self.assertEqual(cm.exception.message, "pathspec 'nope.txt' did not match")

    def test_restore_staged(self):
        self.write("a.txt", "dirty\n")
        self.add("a.txt")
        ops.restore(self.repo, ["a.txt"], self.wt, staged=True)
        self.assertEqual(self.porcelain(), " M a.txt\n")
        self.assertEqual(self.read("a.txt"), b"dirty\n")
        self.write("new.txt", "n\n")
        self.add("new.txt")
        ops.restore(self.repo, ["new.txt"], self.wt, staged=True)
        self.assertIn("?? new.txt", self.porcelain())
        self.assertTrue(self.exists("new.txt"))

    def test_restore_source_and_both(self):
        ops.restore(self.repo, ["a.txt"], self.wt, source=self.c1)
        self.assertEqual(self.read("a.txt"), b"1\n")
        self.assertEqual(self.porcelain(), " M a.txt\n")
        ops.restore(self.repo, ["a.txt"], self.wt, staged=True, worktree=True, source="HEAD")
        self.assertEqual(self.read("a.txt"), b"2\n")
        self.assertEqual(self.porcelain(), "")
        ops.restore(self.repo, ["a.txt"], self.wt, staged=True, worktree=True, source=self.c1)
        self.assertEqual(self.porcelain(), "M  a.txt\n")
        self.assertEqual(self.read("a.txt"), b"1\n")


# =========================================================================== config / plumbing

class ConflictResolutionTests(RepoCase):
    def conflicted_merge(self):
        from minigit import merge as mergemod
        self.write("f.txt", "base\n")
        self.write("ours_del.txt", "base\n")
        self.write("theirs_del.txt", "base\n")
        self.add(".")
        self.commit("base")
        ops.switch(self.repo, None, create="feature")
        self.write("f.txt", "theirs\n")
        self.write("ours_del.txt", "modified\n")
        ops.rm(self.repo, ["theirs_del.txt"], self.wt)
        self.add(".")
        self.commit("feature")
        ops.switch(self.repo, "main")
        self.write("f.txt", "ours\n")
        self.write("theirs_del.txt", "modified\n")
        ops.rm(self.repo, ["ours_del.txt"], self.wt)
        self.add(".")
        self.commit("main")
        self.assertEqual(mergemod.merge(self.repo, "feature").kind, "conflict")

    def test_rm_resolves_du_and_ud_conflicts_without_force(self):
        self.conflicted_merge()
        codes = dict((p, xy) for xy, p in status.compute_status(self.repo).entries)
        self.assertEqual(codes, {"f.txt": "UU", "ours_del.txt": "DU", "theirs_del.txt": "UD"})
        ops.rm(self.repo, ["ours_del.txt"], self.wt)
        ops.rm(self.repo, ["theirs_del.txt"], self.wt)
        self.assertFalse(self.exists("ours_del.txt"))
        self.assertFalse(self.exists("theirs_del.txt"))
        self.assertEqual(self.porcelain(), "UU f.txt\nD  theirs_del.txt\n")

    def test_restore_refuses_an_unmerged_path(self):
        self.conflicted_merge()
        before = self.read("f.txt")
        with self.assertRaises(MinigitError) as cm:
            ops.restore(self.repo, ["f.txt"], self.wt)
        self.assertEqual(cm.exception.message, "path 'f.txt' is unmerged")
        self.assertEqual(self.read("f.txt"), before)
        # taking our version explicitly works and resolves the path
        ops.restore(self.repo, ["f.txt"], self.wt, staged=True, worktree=True, source="HEAD")
        self.assertEqual(self.read("f.txt"), b"ours\n")
        self.assertNotIn("f.txt", read_index(self.repo).unmerged())


class RobustnessTests(RepoCase):
    def test_corrupt_head_is_an_error_and_never_overwritten(self):
        self.commit_file("a.txt", "1\n")
        head_path = os.path.join(self.wt, ".minigit", "HEAD")
        with open(head_path, "wb") as f:
            f.write(b"garbage\n")
        with self.assertRaises(MinigitError) as cm:
            status.compute_status(self.repo)
        self.assertIn("HEAD is corrupt", cm.exception.message)
        self.write("a.txt", "2\n")
        with self.assertRaises(MinigitError):
            ops.commit(self.repo, ["x"], all_=True)
        with open(head_path, "rb") as f:
            self.assertEqual(f.read(), b"garbage\n")

    def test_utf8_bom_in_config_is_skipped(self):
        cfg = os.environ["MINIGIT_GLOBAL_CONFIG"]
        with open(cfg, "wb") as f:
            f.write(b"\xef\xbb\xbf[user]\n\tname = Bom User\n\temail = bom@example.com\n")
        self.assertEqual(ops.config_cmd(self.repo, "user.name", None), "Bom User")
        with open(os.path.join(self.wt, ".minigit", "config"), "rb") as f:
            local = f.read()
        with open(os.path.join(self.wt, ".minigit", "config"), "wb") as f:
            f.write(b"\xef\xbb\xbf" + local)
        self.assertEqual(self.repo.config.get("core.bare"), "false")

    def test_malformed_ipv6_url_is_a_clean_error(self):
        from minigit.transport import split_url
        with self.assertRaises(MinigitError) as cm:
            split_url("http://alice:tok@[::1/proj")
        self.assertIn("invalid remote url", cm.exception.message)

    @unittest.skipUnless(os.name == "nt", "the mode comes from HEAD only where there is no exec bit")
    def test_exec_bit_kept_after_rm_cached_and_readd(self):
        self.commit_file("run.sh", "echo hi\n")
        with locked_index(self.repo) as index:
            e = index.get("run.sh")
            index.put(IndexEntry("run.sh", "100755", e.sha, e.size, e.mtime_ns, 0))
        self.commit("make executable")
        self.assertEqual(self.repo.head_flat()["run.sh"][0], "100755")
        ops.rm(self.repo, ["run.sh"], self.wt, cached=True)
        self.add("run.sh")
        self.assertEqual(read_index(self.repo).get("run.sh").mode, "100755")
        self.assertEqual(self.porcelain(), "")
        ops.rm(self.repo, ["."], self.wt, cached=True, recursive=True)
        self.add(".")
        self.assertEqual(read_index(self.repo).get("run.sh").mode, "100755")


class ConfigPlumbingTests(RepoCase):
    def test_config_roundtrip(self):
        self.assertIsNone(ops.config_cmd(self.repo, "user.name", "Local"))
        self.assertEqual(ops.config_cmd(self.repo, "user.name", None), "Local")
        ops.config_cmd(self.repo, "user.name", "Global", global_=True)
        self.assertEqual(ops.config_cmd(self.repo, "user.name", None), "Local")
        self.assertEqual(ops.config_cmd(self.repo, "user.name", None, global_=True), "Global")
        ops.config_cmd(self.repo, "remote.origin.url", "http://x/y")
        listing = ops.config_cmd(self.repo, None, None, list_=True)
        self.assertIn("remote.origin.url=http://x/y\n", listing)
        self.assertIn("user.name=Local\n", listing)
        self.assertIn("core.bare=false\n", listing)
        ops.config_cmd(self.repo, "user.name", None, unset=True)
        self.assertEqual(ops.config_cmd(self.repo, "user.name", None), "Global")
        with self.assertRaises(MinigitError):
            ops.config_cmd(self.repo, "user.nothere", None)
        with self.assertRaises(MinigitError):
            ops.config_cmd(self.repo, "nodot", "v")
        with self.assertRaises(UsageError):
            ops.config_cmd(self.repo, None, None)
        self.assertEqual(ops.config_cmd(None, "user.name", None, global_=True), "Global")
        with self.assertRaises(MinigitError):
            ops.config_cmd(None, "user.name", "x")

    def test_cat_file_ls_files_hash_object(self):
        self.write("a.txt", "hello\n")
        self.assertEqual(ops.hash_object_cmd(None, os.path.join(self.wt, "a.txt"), False),
                         "ce013625030ba8dba906f756967f9e9ca394464a")
        self.assertFalse(self.repo.objects.has("ce013625030ba8dba906f756967f9e9ca394464a"))
        ops.hash_object_cmd(self.repo, os.path.join(self.wt, "a.txt"), True)
        self.assertTrue(self.repo.objects.has("ce013625030ba8dba906f756967f9e9ca394464a"))
        with self.assertRaises(MinigitError):
            ops.hash_object_cmd(None, os.path.join(self.wt, "missing"), False)
        self.add("a.txt")
        sha = self.commit("c")
        self.assertEqual(ops.cat_file(self.repo, "-t", "HEAD"), b"commit\n")
        self.assertEqual(ops.cat_file(self.repo, "-t", "HEAD:a.txt"), b"blob\n")
        self.assertEqual(ops.cat_file(self.repo, "-s", "HEAD:a.txt"), b"6\n")
        self.assertEqual(ops.cat_file(self.repo, "-p", "HEAD:a.txt"), b"hello\n")
        self.assertTrue(ops.cat_file(self.repo, "-p", sha).startswith(b"tree "))
        self.assertIn(b"100644 blob ce013625030ba8dba906f756967f9e9ca394464a\ta.txt\n",
                      ops.cat_file(self.repo, "-p", "HEAD:"))
        with self.assertRaises(UsageError):
            ops.cat_file(self.repo, "-x", "HEAD")
        self.assertEqual(ops.ls_files(self.repo), "a.txt\n")
        self.assertEqual(ops.ls_files(self.repo, stage=True),
                         "100644 ce013625030ba8dba906f756967f9e9ca394464a 0\ta.txt\n")
        with locked_index(self.repo) as index:
            index.put(IndexEntry("a.txt", "100644", hash_object("blob", b"o\n"), stage=2))
        self.assertIn(" 2\ta.txt\n", ops.ls_files(self.repo, stage=True))
        self.assertEqual(ops.ls_files(self.repo), "a.txt\n")

    def test_empty_tree_fixture(self):
        self.assertEqual(EMPTY_TREE, "4b825dc642cb6eb9a060e54bf8d69288fbee4904")


if __name__ == "__main__":
    unittest.main()


# =========================================================================== status display

class StatusViewTests(RepoCase):
    def view(self, cwd=None):
        st = ops.status_view(self.repo, status.compute_status(self.repo), cwd or self.wt)
        return status.format_status_porcelain(st)

    def test_tracked_change_listed_before_untracked_for_same_path(self):
        self.commit_file("s.txt", "s\n")
        ops.rm(self.repo, ["s.txt"], self.wt, cached=True)
        self.assertEqual(self.view(), "D  s.txt\n?? s.txt\n")

    def test_untracked_directories_collapse(self):
        self.commit_file("sub/t.txt", "t\n")
        self.write("e/1", "1\n")
        self.write("e/deep/2", "2\n")
        self.write("sub/new/x", "x\n")
        self.write("sub/u.txt", "u\n")
        self.write("top.txt", "t\n")
        self.assertEqual(self.view(), "?? e/\n?? sub/new/\n?? sub/u.txt\n?? top.txt\n")
        # the raw porcelain stays one line per file (SPEC)
        self.assertIn("?? e/1\n?? e/deep/2\n", self.porcelain())

    def test_paths_relative_to_cwd(self):
        self.commit_file("sub/t.txt", "t\n")
        self.write("sub/t.txt", "changed\n")
        self.write("sub/deep/a.txt", "a\n")
        self.write("top.txt", "b\n")
        sub = os.path.join(self.wt, "sub")
        self.assertEqual(self.view(sub), " M t.txt\n?? deep/\n?? ../top.txt\n")  # repo-path order, like git
        long = status.format_status_long(ops.status_view(self.repo, status.compute_status(self.repo), sub))
        self.assertIn("\tmodified:   t.txt\n", long)
        self.assertIn("\t../top.txt\n", long)


# =========================================================================== CLI semantics

class CliSemanticsTests(unittest.TestCase):
    """Command-line behaviour (argument parsing) checked through the real CLI."""

    def setUp(self):
        from tests.helpers import make_env, run
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.env = make_env(self._tmp.name)
        self.wt = os.path.join(self._tmp.name, "r")
        os.makedirs(self.wt)
        self._run = run
        self.mg("init")

    def mg(self, *args, check=True, cwd=None):
        return self._run(list(args), cwd=cwd or self.wt, env=self.env, check=check)

    def commit(self, name, data, msg):
        with open(os.path.join(self.wt, name), "w", encoding="utf-8") as f:
            f.write(data)
        self.mg("add", name)
        self.mg("commit", "-m", msg)

    def test_log_minus_n_shorthand(self):
        self.commit("a.txt", "1\n", "one")
        self.commit("a.txt", "2\n", "two")
        out = self.mg("log", "-1", "--oneline").out
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("two", out)
        self.assertEqual(self.mg("log", "--oneline", "-2").out.count("\n"), 2)

    def test_unknown_option_prints_the_subcommand_usage(self):
        for args, prog in ((["commit", "--signoff", "-m", "x"], "minigit commit"),
                           (["reset", "-q"], "minigit reset"),
                           (["rev-parse", "--abbrev-ref", "HEAD"], "minigit rev-parse")):
            with self.subTest(args=args):
                res = self.mg(*args, check=False)
                self.assertEqual(res.returncode, 2)
                self.assertIn(f"usage: {prog}", res.err)
                self.assertIn("unrecognized arguments", res.err)

    def test_branch_after_dashdash_and_invalid_names(self):
        self.commit("a.txt", "1\n", "one")
        self.mg("branch", "--", "foo")
        self.assertIn("foo", self.mg("branch").out)
        res = self.mg("branch", "HEAD", check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("'HEAD' is not a valid branch name", res.err)
        self.assertFalse(os.path.exists(os.path.join(self.wt, ".minigit", "refs", "heads", "HEAD")))

    def test_checkout_head_keeps_the_branch_attached(self):
        self.commit("a.txt", "1\n", "one")
        self.mg("checkout", "HEAD")
        with open(os.path.join(self.wt, ".minigit", "HEAD"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "ref: refs/heads/main")

    def test_switch_to_a_commit_needs_detach(self):
        self.commit("a.txt", "1\n", "one")
        self.commit("a.txt", "2\n", "two")
        res = self.mg("switch", "HEAD~1", check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("a branch is expected, got commit 'HEAD~1'", res.err)
        self.mg("switch", "--detach", "HEAD~1")
        self.assertTrue(self.mg("branch").out.startswith("* (HEAD detached at "))

    def test_status_short_from_a_subdirectory(self):
        os.makedirs(os.path.join(self.wt, "sub", "deep"))
        for rel in ("sub/a.txt", "top.txt", "e/1"):
            p = os.path.join(self.wt, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("x\n")
        sub = os.path.join(self.wt, "sub")
        # sub/ itself holds nothing tracked, so git shows it as "./"
        self.assertEqual(self.mg("status", "-s", cwd=sub).out, "?? ../e/\n?? ./\n?? ../top.txt\n")
        self.assertEqual(self.mg("status", "--porcelain", cwd=sub).out, "?? e/1\n?? sub/a.txt\n?? top.txt\n")

"""Tests for ``minigit gc`` (minigit/gc.py) and the temporary-file warning of ``fsck``.

Repositories are built with the library; the command itself is driven through the real CLI
(``python -m minigit``). Run from the project root::

    python -m unittest tests.test_gc -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from minigit import gc as gc_mod
from minigit import pack
from minigit.objects import Commit, Signature, serialize_commit
from minigit.repo import Repository
from minigit.trees import build_tree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = time.time() - 2 * 3600          # older than gc's one-hour grace period
SIG = Signature("Test User", "test@example.com", 1700000000, 0)


def _env(home: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["HOME"] = env["USERPROFILE"] = home
    env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(home, ".minigitconfig")
    env["MINIGIT_AUTHOR_NAME"] = "Test User"
    env["MINIGIT_AUTHOR_EMAIL"] = "test@example.com"
    env["MINIGIT_AUTHOR_DATE"] = "1700000000 +0000"
    env.pop("MINIGIT_DEBUG", None)
    return env


class GcTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        self.env = _env(home)
        self.work = os.path.join(self.tmp, "repo")
        self.repo, _ = Repository.init(self.work)
        self.store = self.repo.objects
        self.main = self.commit({"a.txt": b"a\n"}, [], "A")
        self.repo.refs.update("refs/heads/main", self.main)

    # -- helpers ------------------------------------------------------------
    def mg(self, *args, cwd=None, code=0):
        p = subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd or self.work, env=self.env,
                           capture_output=True, timeout=60)
        out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        self.assertEqual(p.returncode, code, f"minigit {' '.join(args)}\n{out}\n{err}")
        return out, err

    def commit(self, files: dict, parents: list, message: str, store=None) -> str:
        """Write blobs, tree and commit; return the commit id (every object's mtime is left as written)."""
        store = store or self.store
        flat = {p: ("100644", store.write("blob", data)) for p, data in files.items()}
        tree = build_tree(store, flat)
        return store.write("commit", serialize_commit(Commit(tree, tuple(parents), SIG, SIG, message + "\n")))

    def age(self, store=None):
        """Make every object of the store older than the grace period."""
        store = store or self.store
        for sha in store.iter_all():
            os.utime(store.path(sha), (OLD, OLD))

    def temp_file(self, name="tmp-4242-deadbeef", old=False, store=None) -> str:
        store = store or self.store
        d = os.path.join(store.dir, "ab")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "wb") as fh:
            fh.write(b"half an object")
        if old:
            os.utime(path, (OLD, OLD))
        return path

    def objects(self, store=None) -> set:
        return set((store or self.store).iter_all())

    # -- temporary files ------------------------------------------------------
    def test_nothing_to_clean(self):
        out, _ = self.mg("gc")
        self.assertEqual(out, "Nothing to clean\n")
        out, _ = self.mg("gc", "--prune", "--force")
        self.assertEqual(out, "Nothing to clean\n")

    def test_fresh_temp_file_is_kept_until_forced(self):
        path = self.temp_file()
        out, _ = self.mg("gc")
        self.assertEqual(out, "Nothing to clean\n")
        self.assertTrue(os.path.exists(path))
        out, _ = self.mg("gc", "--force")
        self.assertEqual(out, "Removed 1 temporary files\n")
        self.assertFalse(os.path.exists(path))

    def test_old_temp_files_are_removed(self):
        old1 = self.temp_file("tmp-1-00000001", old=True)
        old2 = self.temp_file("tmp-2-00000002", old=True)
        fresh = self.temp_file("tmp-3-00000003")
        self.assertEqual(gc_mod.gc(self.repo), (2, 0))
        self.assertFalse(os.path.exists(old1) or os.path.exists(old2))
        self.assertTrue(os.path.exists(fresh))
        # objects are never touched without --prune, however old and unreachable
        self.age()
        loose = self.commit({"x.txt": b"x\n"}, [], "loose")
        self.age()
        out, _ = self.mg("gc", "--force")
        self.assertEqual(out, "Removed 1 temporary files\n")
        self.assertTrue(self.store.has(loose))

    def test_fsck_warns_about_temp_files_and_still_passes(self):
        self.temp_file("tmp-4242-deadbeef")
        self.assertEqual(pack.fsck(self.repo),
                         ["warning: temporary file objects/ab/tmp-4242-deadbeef (run 'minigit gc')"])
        out, err = self.mg("fsck")
        self.assertEqual(out, "warning: temporary file objects/ab/tmp-4242-deadbeef (run 'minigit gc')\n")
        self.assertEqual(err, "")
        self.mg("gc", "--force")
        self.assertEqual(pack.fsck(self.repo), [])
        self.assertEqual(self.mg("fsck"), ("", ""))

    # -- --prune ----------------------------------------------------------------
    def test_prune_deletes_old_unreachable_objects_only(self):
        before = self.objects()
        dangling = self.commit({"d.txt": b"dangling\n"}, [self.main], "dangling")   # commit + tree + blob
        self.age()
        fresh_blob = self.store.write("blob", b"just written\n")
        out, _ = self.mg("gc", "--prune")
        self.assertEqual(out, "Pruned 3 unreachable objects\n")
        self.assertEqual(self.objects(), before | {fresh_blob})
        self.assertFalse(self.store.has(dangling))
        self.assertEqual(pack.fsck(self.repo), [])
        out, _ = self.mg("gc", "--prune", "--force")
        self.assertEqual(out, "Pruned 1 unreachable objects\n")
        self.assertEqual(self.objects(), before)

    def test_both_counts_are_reported_in_order(self):
        self.temp_file(old=True)
        self.commit({"d.txt": b"dangling\n"}, [], "dangling")
        self.age()
        out, _ = self.mg("gc", "--prune")
        self.assertEqual(out, "Removed 1 temporary files\nPruned 3 unreachable objects\n")

    def test_prune_keeps_everything_a_root_names(self):
        repo, store = self.repo, self.store
        # a staged blob that is in no commit yet (the index is a root too); first, because reset and add
        # rewrite ORIG_HEAD and clear the operation state files written below
        self.mg("reset", "--hard", "HEAD")
        with open(os.path.join(self.work, "staged.txt"), "wb") as fh:
            fh.write(b"staged only\n")
        self.mg("add", "staged.txt")
        staged = self.store.write("blob", b"staged only\n")   # same id, already there
        keep = {}
        keep["branch"] = self.commit({"b.txt": b"b\n"}, [self.main], "branch")
        repo.refs.update("refs/heads/feature", keep["branch"])
        keep["remote"] = self.commit({"r.txt": b"r\n"}, [self.main], "remote")
        repo.refs.update("refs/remotes/origin/main", keep["remote"])
        for name in ("ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD"):
            keep[name] = self.commit({name: name.encode()}, [], name)
            repo.refs.write_special(name, keep[name])
        keep["stash"] = self.commit({"s.txt": b"s\n"}, [self.main], "stash")
        repo.write_state("stash", (keep["stash"] + "\n").encode())
        keep["rebase-orig"] = self.commit({"o.txt": b"o\n"}, [], "rebase orig")
        keep["rebase-todo"] = self.commit({"t.txt": b"t\n"}, [], "rebase todo")
        keep["rebase-done"] = self.commit({"n.txt": b"n\n"}, [], "rebase done")
        repo.write_state("rebase/head-name", b"refs/heads/main\n")
        repo.write_state("rebase/orig-head", (keep["rebase-orig"] + "\n").encode())
        repo.write_state("rebase/onto", (self.main + "\n").encode())
        repo.write_state("rebase/todo", (keep["rebase-todo"] + "\n").encode())
        repo.write_state("rebase/done", (keep["rebase-done"] + "\n").encode())
        keep["seq-orig"] = self.commit({"q.txt": b"q\n"}, [], "sequencer orig")
        keep["seq-todo"] = self.commit({"w.txt": b"w\n"}, [], "sequencer todo")
        repo.write_state("SEQUENCER", json.dumps({"action": "cherry-pick", "orig_head": keep["seq-orig"],
                                                  "todo": [keep["seq-todo"]], "opts": {}}).encode())
        dangling = self.commit({"gone.txt": b"gone\n"}, [], "dangling")
        self.age()

        roots = gc_mod.roots(repo)
        for name, sha in keep.items():
            self.assertIn(sha, roots, name)
        self.assertIn(staged, roots)
        self.assertNotIn(dangling, roots)
        out, _ = self.mg("gc", "--prune")
        self.assertEqual(out, "Pruned 3 unreachable objects\n")
        for name, sha in keep.items():
            self.assertTrue(store.has(sha), name)
        self.assertTrue(store.has(staged))
        self.assertFalse(store.has(dangling))

    def test_detached_head_is_a_root(self):
        detached = self.commit({"d.txt": b"d\n"}, [self.main], "detached")
        self.repo.refs.set_detached_head(detached)
        self.age()
        out, _ = self.mg("gc", "--prune")
        self.assertEqual(out, "Nothing to clean\n")
        self.assertTrue(self.store.has(detached))

    def test_prune_deletes_nothing_when_a_walk_fails(self):
        self.repo.refs.update("refs/heads/broken", "b" * 40)
        tmp = self.temp_file(old=True)
        dangling = self.commit({"d.txt": b"dangling\n"}, [], "dangling")
        self.age()
        out, err = self.mg("gc", "--prune", code=1)
        self.assertEqual(out, "")
        self.assertIn("error: cannot prune: object " + "b" * 40 + " not found", err)
        self.assertIn("hint: nothing was deleted", err)
        self.assertTrue(os.path.exists(tmp))
        self.assertTrue(self.store.has(dangling))
        # a state file that is not made of commit ids refuses too
        self.repo.refs.update("refs/heads/broken", None)
        self.repo.write_state("stash", b"not-a-sha\n")
        _, err = self.mg("gc", "--prune", code=1)
        self.assertIn("error: cannot prune: .minigit/stash line 1 is not a commit id", err)
        self.assertTrue(self.store.has(dangling))
        # without --prune the broken state does not matter: temporary files are still cleaned
        out, _ = self.mg("gc")
        self.assertEqual(out, "Removed 1 temporary files\n")

    def test_bare_repository(self):
        bare_dir = os.path.join(self.tmp, "bare")
        bare, _ = Repository.init(bare_dir, bare=True)
        tip = self.commit({"a.txt": b"a\n"}, [], "A", store=bare.objects)
        bare.refs.update("refs/heads/main", tip)
        self.commit({"z.txt": b"z\n"}, [], "dangling", store=bare.objects)
        self.temp_file(old=True, store=bare.objects)
        self.age(bare.objects)
        out, _ = self.mg("gc", "--prune", cwd=bare_dir)
        self.assertEqual(out, "Removed 1 temporary files\nPruned 3 unreachable objects\n")
        self.assertEqual(pack.fsck(bare), [])
        self.assertTrue(bare.objects.has(tip))

    def test_usage(self):
        _, err = self.mg("gc", "extra", code=2)
        self.assertIn("usage: minigit gc", err)
        out, _ = self.mg("gc", "--help")
        self.assertIn("--prune", out)
        out, _ = self.mg("--help")
        self.assertIn("gc", out)


if __name__ == "__main__":
    unittest.main()

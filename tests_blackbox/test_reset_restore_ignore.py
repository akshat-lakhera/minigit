"""Black-box tests for reset (--soft/--mixed/--hard, path form), restore / checkout -- paths,
and .minigitignore handling.

Every test drives the real CLI (``python -m minigit``) as a subprocess inside a fresh temporary
directory. Expectations come from SPEC.md, and from real git where the spec is silent.

Run from E:\\ideas\\minigit with:
    python -m unittest tests_blackbox.test_reset_restore_ignore
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 60


@dataclass
class Result:
    args: tuple
    code: int
    raw: bytes
    rawerr: bytes

    @property
    def out(self) -> str:
        return self.raw.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.rawerr.decode("utf-8", "replace")

    def __str__(self) -> str:
        return (f"minigit {' '.join(map(str, self.args))} -> exit {self.code}\n"
                f"--- stdout ---\n{self.out}--- stderr ---\n{self.err}")


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class MinigitCase(unittest.TestCase):
    """Fresh repo per test at self.repo, with an isolated identity/config/home."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mg_rri_", ignore_cleanup_errors=True)
        self.tmp = self._tmp.name
        self.home = os.path.join(self.tmp, "home")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.home)
        os.makedirs(self.repo)
        env = {k: v for k, v in os.environ.items() if not k.startswith("MINIGIT_")}
        prev = env.get("PYTHONPATH")
        env["PYTHONPATH"] = ROOT if not prev else ROOT + os.pathsep + prev
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "HOME": self.home,
            "USERPROFILE": self.home,
            "MINIGIT_GLOBAL_CONFIG": os.path.join(self.home, ".minigitconfig"),
            "MINIGIT_CREDENTIALS": os.path.join(self.home, ".minigit-credentials.json"),
            "MINIGIT_AUTHOR_NAME": "QA Tester",
            "MINIGIT_AUTHOR_EMAIL": "qa@example.com",
        })
        self.env = env
        self._date = 1_700_000_000
        self.mg("init")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- running the CLI ---------------------------------------------------------------
    def mg(self, *args: str, cwd: str | None = None, code: int | None = 0) -> Result:
        """Run minigit. ``code`` is the required exit code (None = any)."""
        env = dict(self.env)
        self._date += 60  # distinct, increasing commit times
        env["MINIGIT_AUTHOR_DATE"] = f"{self._date} +0000"
        p = subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd or self.repo, env=env,
                           capture_output=True, timeout=TIMEOUT)
        r = Result(args, p.returncode, p.stdout, p.stderr)
        self.assertNotIn("internal error", r.err, f"crash:\n{r}")
        self.assertNotIn("Traceback", r.err, f"crash:\n{r}")
        if code is not None:
            self.assertEqual(r.code, code, f"unexpected exit code:\n{r}")
        return r

    # ---- worktree helpers --------------------------------------------------------------
    def p(self, rel: str) -> str:
        return os.path.join(self.repo, *rel.split("/"))

    def write(self, rel: str, data: str | bytes) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        path = self.p(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def read(self, rel: str) -> bytes:
        with open(self.p(rel), "rb") as f:
            return f.read()

    def exists(self, rel: str) -> bool:
        return os.path.lexists(self.p(rel))

    def delete(self, rel: str) -> None:
        path = self.p(rel)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    def gitfile(self, name: str) -> str:
        return os.path.join(self.repo, ".minigit", name)

    def special(self, name: str) -> str | None:
        try:
            with open(self.gitfile(name), "rb") as f:
                return f.read().decode().strip()
        except FileNotFoundError:
            return None

    # ---- repo queries ------------------------------------------------------------------
    def status(self, cwd: str | None = None) -> list[str]:
        out = self.mg("status", "--porcelain", cwd=cwd).out
        return sorted(line.rstrip("\r") for line in out.split("\n") if line.strip())

    def assertStatus(self, expected: list[str], msg: str | None = None) -> None:
        self.assertEqual(self.status(), sorted(expected), msg)

    def rev(self, spec: str) -> str:
        return self.mg("rev-parse", spec).out.strip()

    def head(self) -> str:
        return self.rev("HEAD")

    def ls_files(self) -> list[str]:
        return [l for l in self.mg("ls-files").out.splitlines() if l]

    def ls_stage(self) -> dict[str, list[tuple[str, str, int]]]:
        res: dict[str, list[tuple[str, str, int]]] = {}
        for line in self.mg("ls-files", "--stage").out.splitlines():
            if not line:
                continue
            meta, path = line.split("\t", 1)
            mode, sha, stage = meta.split()
            res.setdefault(path, []).append((mode, sha, int(stage)))
        return res

    def commit_all(self, msg: str) -> str:
        self.mg("add", ".")
        self.mg("commit", "-m", msg)
        return self.head()

    def tree_of(self, rev: str) -> str:
        out = self.mg("cat-file", "-p", rev).out
        for line in out.splitlines():
            if line.startswith("tree "):
                return line.split()[1]
        self.fail(f"no tree line in commit {rev}: {out!r}")

    def make_conflict(self) -> str:
        """Merge 'other' into main leaving two conflicts: f.txt (content, UU) and d.txt
        (modify/delete: deleted on main, modified on other). Returns ours sha."""
        self.write("f.txt", "base\n")
        self.write("d.txt", "doomed\n")
        self.write("g.txt", "untouched\n")
        self.commit_all("base")
        self.mg("switch", "-c", "other")
        self.write("f.txt", "theirs\n")
        self.write("d.txt", "modified by them\n")
        self.commit_all("theirs")
        self.mg("switch", "main")
        self.write("f.txt", "ours\n")
        self.mg("rm", "d.txt")
        ours = self.commit_all("ours")
        self.mg("merge", "other", code=1)
        self.assertIsNotNone(self.special("MERGE_HEAD"), "test setup: merge should conflict")
        self.assertIn(b"<<<<<<<", self.read("f.txt"))
        self.assertTrue(self.exists("d.txt"), "test setup: modify/delete keeps their side on disk")
        return ours


# =========================================================================================
# reset
# =========================================================================================
class ResetTests(MinigitCase):

    def test_soft_reset_moves_head_only_then_commit_squashes(self):
        self.write("a.txt", "one\n")
        self.write("c.txt", "same\n")
        c1 = self.commit_all("first")
        self.write("a.txt", "two\n")
        self.write("b.txt", "new\n")
        c2 = self.commit_all("second")

        self.mg("reset", "--soft", "HEAD~1")
        self.assertEqual(self.head(), c1)
        self.assertEqual(self.special("ORIG_HEAD"), c2)
        self.assertEqual(self.read("a.txt"), b"two\n")
        self.assertEqual(self.read("b.txt"), b"new\n")
        self.assertStatus(["M  a.txt", "A  b.txt"])

        self.mg("commit", "-m", "squashed")
        self.assertEqual(self.rev("HEAD~1"), c1)
        self.assertEqual(self.tree_of("HEAD"), self.tree_of(c2))
        self.assertStatus([])

    def test_mixed_reset_is_default_and_keeps_worktree(self):
        self.write("a.txt", "one\n")
        self.write("c.txt", "same\n")
        c1 = self.commit_all("first")
        self.write("a.txt", "two\n")
        self.write("b.txt", "new\n")
        c2 = self.commit_all("second")

        self.mg("reset", "HEAD~1")
        self.assertEqual(self.head(), c1)
        self.assertEqual(self.special("ORIG_HEAD"), c2)
        self.assertEqual(self.read("a.txt"), b"two\n")
        self.assertEqual(self.read("b.txt"), b"new\n")
        self.assertEqual(self.ls_files(), ["a.txt", "c.txt"])
        # c.txt is unchanged and must not show up as modified
        self.assertStatus([" M a.txt", "?? b.txt"])

        self.mg("reset", "--mixed", "ORIG_HEAD")
        self.assertEqual(self.head(), c2)
        self.assertStatus([])

    def test_hard_reset_restores_tracked_and_removes_later_files(self):
        self.write("a.txt", "one\n")
        self.write("sub/keep.txt", "keep\n")
        c1 = self.commit_all("first")
        self.write("a.txt", "two\n")
        self.write("b.txt", "new\n")
        self.write("sub/new/deep.txt", "deep\n")
        self.commit_all("second")

        r = self.mg("reset", "--hard", "HEAD~1")
        self.assertIn(f"HEAD is now at {c1[:7]} first", r.out)
        self.assertEqual(self.head(), c1)
        self.assertEqual(self.read("a.txt"), b"one\n")
        self.assertFalse(self.exists("b.txt"))
        self.assertFalse(self.exists("sub/new/deep.txt"))
        self.assertFalse(self.exists("sub/new"), "emptied directory should be pruned")
        self.assertEqual(self.read("sub/keep.txt"), b"keep\n")
        self.assertStatus([])

    def test_hard_reset_never_touches_untracked_or_ignored_files(self):
        self.write(".minigitignore", "*.log\n")
        self.write("a.txt", "one\n")
        self.commit_all("first")
        self.write("a.txt", "two\n")
        self.write("gen/out.txt", "generated\n")
        self.commit_all("second")
        self.write("u.txt", "untracked\n")
        self.write("gen/untracked.txt", "mine\n")   # lives in a dir whose tracked file goes away
        self.write("debug.log", "ignored\n")
        self.write("gen/x.log", "ignored too\n")

        self.mg("reset", "--hard", "HEAD~1")
        self.assertFalse(self.exists("gen/out.txt"))
        self.assertEqual(self.read("u.txt"), b"untracked\n")
        self.assertEqual(self.read("gen/untracked.txt"), b"mine\n")
        self.assertEqual(self.read("debug.log"), b"ignored\n")
        self.assertEqual(self.read("gen/x.log"), b"ignored too\n")
        self.assertStatus(["?? gen/untracked.txt", "?? u.txt"])

    def test_hard_reset_deletes_staged_new_file_but_keeps_unstaged_untracked(self):
        self.write("a.txt", "one\n")
        self.commit_all("first")
        self.write("new.txt", "staged but never committed\n")
        self.mg("add", "new.txt")
        self.write("other.txt", "plain untracked\n")

        self.mg("reset", "--hard")
        self.assertFalse(self.exists("new.txt"), "a staged new file is discarded by reset --hard (git)")
        self.assertEqual(self.read("other.txt"), b"plain untracked\n")
        self.assertStatus(["?? other.txt"])

    def test_hard_reset_discards_staged_unstaged_and_deleted_changes(self):
        self.write("a.txt", "one\n")
        self.write("src/x/y.txt", "y\n")
        self.write("src/z.txt", "z\n")
        self.write("del.txt", "del\n")
        c1 = self.commit_all("first")
        self.write("a.txt", "staged\n")
        self.mg("add", "a.txt")
        self.write("a.txt", "worktree\n")
        self.delete("src")
        self.mg("rm", "del.txt")
        self.assertFalse(self.exists("del.txt"))

        self.mg("reset", "--hard")
        self.assertEqual(self.head(), c1)
        self.assertEqual(self.read("a.txt"), b"one\n")
        self.assertEqual(self.read("src/x/y.txt"), b"y\n")
        self.assertEqual(self.read("src/z.txt"), b"z\n")
        self.assertEqual(self.read("del.txt"), b"del\n")
        self.assertStatus([])

    def test_orig_head_undo_and_revision_forms(self):
        shas = []
        for i in (1, 2, 3):
            self.write("a.txt", f"v{i}\n")
            shas.append(self.commit_all(f"c{i}"))
        c1, c2, c3 = shas

        self.mg("reset", "--hard", "HEAD~2")
        self.assertEqual(self.head(), c1)
        self.assertEqual(self.read("a.txt"), b"v1\n")
        self.assertEqual(self.special("ORIG_HEAD"), c3)

        self.mg("reset", "--hard", "ORIG_HEAD")
        self.assertEqual(self.head(), c3)
        self.assertEqual(self.read("a.txt"), b"v3\n")

        self.mg("reset", "--hard", c2[:8])
        self.assertEqual(self.head(), c2)
        self.assertEqual(self.read("a.txt"), b"v2\n")

        self.mg("branch", "keep", c1)
        self.mg("reset", "--hard", "keep")
        self.assertEqual(self.head(), c1)
        self.mg("reset", "--hard", "@")
        self.assertEqual(self.head(), c1)
        # HEAD stays attached; the branch itself moved
        self.assertEqual(self.special("HEAD"), "ref: refs/heads/main")
        self.assertEqual(self.special("refs/heads/main"), c1)
        self.assertStatus([])

    def test_reset_errors_change_nothing(self):
        self.write("a.txt", "one\n")
        self.commit_all("first")
        self.write("a.txt", "two\n")
        c2 = self.commit_all("second")
        self.write("a.txt", "local edit\n")
        self.write("b.txt", "staged\n")
        self.mg("add", "b.txt")
        before = self.status()

        r = self.mg("reset", "--hard", "nosuchrev", code=128)   # git: fatal
        self.assertIn("fatal: unknown revision", r.err)
        self.assertEqual(self.head(), c2)
        self.assertEqual(self.read("a.txt"), b"local edit\n")
        self.assertEqual(self.status(), before)

        r = self.mg("reset", "--soft", "HEAD", "--", "a.txt", code=128)
        self.assertTrue(r.err.startswith("fatal: Cannot do soft reset with paths.\n"), r)
        r = self.mg("reset", "--hard", "HEAD", "--", "a.txt", code=128)
        self.assertTrue(r.err.startswith("fatal: Cannot do hard reset with paths.\n"), r)
        r = self.mg("reset", "--", "nope.txt", code=1)
        self.assertIn("did not match", r.err)
        self.assertEqual(self.head(), c2)
        self.assertEqual(self.status(), before)

    def test_soft_reset_refused_during_merge(self):
        ours = self.make_conflict()
        r = self.mg("reset", "--soft", "HEAD~1", code=128)
        self.assertEqual(r.err, "fatal: Cannot do a soft reset in the middle of a merge.\n")
        self.assertEqual(self.head(), ours)
        self.assertIsNotNone(self.special("MERGE_HEAD"))
        self.assertIn("UU f.txt", self.status())

    def test_hard_reset_abandons_conflicted_merge(self):
        ours = self.make_conflict()
        self.mg("reset", "--hard")
        self.assertEqual(self.head(), ours)
        self.assertIsNone(self.special("MERGE_HEAD"))
        self.assertIsNone(self.special("MERGE_MSG"))
        self.assertEqual(self.read("f.txt"), b"ours\n")
        # d.txt was only in the old index (stages 1 and 3), not in HEAD: it must be deleted
        self.assertFalse(self.exists("d.txt"))
        stages = self.ls_stage()
        self.assertEqual(sorted(stages), ["f.txt", "g.txt"])
        for path, entries in stages.items():
            self.assertEqual([e[2] for e in entries], [0], f"{path} still has conflict stages")
        self.assertStatus([])

    def test_mixed_reset_clears_merge_state_but_keeps_conflict_markers(self):
        ours = self.make_conflict()
        self.mg("reset")
        self.assertEqual(self.head(), ours)
        self.assertIsNone(self.special("MERGE_HEAD"))
        self.assertIsNone(self.special("MERGE_MSG"))
        self.assertIn(b"<<<<<<<", self.read("f.txt"))
        stages = self.ls_stage()
        self.assertEqual(stages["f.txt"], [("100644", blob_sha(b"ours\n"), 0)])
        self.assertNotIn("d.txt", stages)
        self.assertEqual(self.read("d.txt"), b"modified by them\n")
        self.assertStatus([" M f.txt", "?? d.txt"])

    def test_reset_paths_unstages_only_named_paths(self):
        self.write("a.txt", "one\n")
        self.write("b.txt", "one\n")
        c1 = self.commit_all("first")
        self.write("a.txt", "two\n")
        self.write("b.txt", "two\n")
        self.mg("add", ".")
        self.assertStatus(["M  a.txt", "M  b.txt"])

        self.mg("reset", "--", "a.txt")
        self.assertStatus([" M a.txt", "M  b.txt"])
        self.mg("reset", "b.txt")           # no "--": an existing path is a path
        self.assertStatus([" M a.txt", " M b.txt"])
        self.assertEqual(self.head(), c1)
        self.assertEqual(self.read("a.txt"), b"two\n")
        self.assertEqual(self.read("b.txt"), b"two\n")

    def test_reset_path_from_older_revision_leaves_head(self):
        self.write("a.txt", "one\n")
        self.commit_all("first")
        self.write("a.txt", "two\n")
        c2 = self.commit_all("second")

        self.mg("reset", "HEAD~1", "--", "a.txt")
        self.assertEqual(self.head(), c2)
        self.assertEqual(self.read("a.txt"), b"two\n")
        self.assertEqual(self.ls_stage()["a.txt"][0][1], blob_sha(b"one\n"))
        self.assertStatus(["MM a.txt"])

    def test_reset_path_unstages_new_file_on_unborn_and_born_branch(self):
        self.write("a.txt", "a\n")
        self.write("b.txt", "b\n")
        self.mg("add", ".")
        self.mg("reset", "--", "a.txt")   # unborn HEAD: allowed, simply unstages
        self.assertStatus(["?? a.txt", "A  b.txt"])
        self.assertEqual(self.read("a.txt"), b"a\n")

        self.mg("commit", "-m", "only b")
        self.write("new.txt", "n\n")
        self.mg("add", "new.txt")
        self.mg("reset", "new.txt")
        self.assertStatus(["?? a.txt", "?? new.txt"])
        self.assertTrue(self.exists("new.txt"))

    def test_reset_and_restore_paths_are_relative_to_cwd(self):
        self.write("x.txt", "root\n")
        self.write("sub/x.txt", "sub\n")
        self.commit_all("first")
        self.write("x.txt", "root2\n")
        self.write("sub/x.txt", "sub2\n")
        self.mg("add", ".")
        sub = self.p("sub")

        self.mg("reset", "--", "x.txt", cwd=sub)
        self.assertStatus(["M  x.txt", " M sub/x.txt"])
        self.mg("restore", "x.txt", cwd=sub)
        self.assertEqual(self.read("sub/x.txt"), b"sub\n")
        self.assertEqual(self.read("x.txt"), b"root2\n")
        self.assertStatus(["M  x.txt"])
        self.mg("restore", "--staged", "../x.txt", cwd=sub)
        self.assertStatus([" M x.txt"])

    def test_hard_reset_handles_file_to_directory_and_back(self):
        self.write("d", "file\n")
        c1 = self.commit_all("d is a file")
        self.mg("rm", "d")
        self.write("d/x", "in dir\n")
        c2 = self.commit_all("d is a dir")

        self.mg("reset", "--hard", c1)
        self.assertTrue(os.path.isfile(self.p("d")))
        self.assertEqual(self.read("d"), b"file\n")
        self.assertStatus([])

        self.mg("reset", "--hard", c2)
        self.assertTrue(os.path.isdir(self.p("d")))
        self.assertEqual(self.read("d/x"), b"in dir\n")
        self.assertStatus([])

    def test_reset_blocked_by_index_lock_changes_nothing(self):
        # Another minigit process holds index.lock. reset must fail without moving HEAD
        # (git takes the index lock before it updates any ref).
        shas = []
        for i in (1, 2, 3):
            self.write("a.txt", f"v{i}\n")
            shas.append(self.commit_all(f"c{i}"))
        c3 = shas[2]
        lock = self.gitfile("index.lock")
        with open(lock, "wb"):
            pass
        for mode in ("--hard", "--mixed"):
            with self.subTest(mode=mode):
                self.mg("reset", "--soft", c3)     # re-arm: soft reset does not need the index
                self.assertEqual(self.head(), c3)
                r = self.mg("reset", mode, "HEAD~1", code=128)
                self.assertIn("index.lock", r.err)
                self.assertEqual(self.head(), c3, f"failed 'reset {mode}' moved HEAD anyway")
        os.remove(lock)
        self.mg("reset", "--soft", c3)
        self.assertEqual(self.read("a.txt"), b"v3\n")
        self.assertStatus([])

    def test_reset_rebuilds_corrupt_index(self):
        self.write("a.txt", "one\n")
        self.write("sub/b.txt", "b\n")
        self.commit_all("first")
        self.write("a.txt", "local\n")
        with open(self.gitfile("index"), "wb") as f:
            f.write(b"this is not an index at all")

        r = self.mg("status", code=1)
        self.assertIn("index file is corrupt", r.err)
        self.mg("reset")
        self.assertEqual(self.ls_files(), ["a.txt", "sub/b.txt"])
        self.assertEqual(self.read("a.txt"), b"local\n")
        self.assertStatus([" M a.txt"])

    def test_hard_reset_run_from_subdirectory_that_disappears(self):
        self.write("a.txt", "a\n")
        c1 = self.commit_all("first")
        self.write("sub/f.txt", "f\n")
        self.commit_all("second")

        self.mg("reset", "--hard", "HEAD~1", cwd=self.p("sub"))
        self.assertEqual(self.head(), c1)
        self.assertFalse(self.exists("sub/f.txt"))
        self.assertStatus([])


# =========================================================================================
# restore / checkout -- paths
# =========================================================================================
class RestoreTests(MinigitCase):

    def test_restore_worktree_from_index_and_deleted_file(self):
        self.write("a.txt", "one\n")
        self.write("b.txt", "bee\n")
        self.commit_all("first")
        self.write("a.txt", "two\n")
        self.mg("add", "a.txt")
        self.write("a.txt", "three\n")
        self.delete("b.txt")

        self.mg("restore", "a.txt", "b.txt")
        self.assertEqual(self.read("a.txt"), b"two\n", "worktree comes from the index, not HEAD")
        self.assertEqual(self.read("b.txt"), b"bee\n")
        self.assertStatus(["M  a.txt"])

    def test_restore_staged_unstages_and_keeps_worktree(self):
        self.write("a.txt", "one\n")
        self.commit_all("first")
        self.write("a.txt", "two\n")
        self.write("new.txt", "n\n")
        self.mg("add", ".")

        self.mg("restore", "--staged", "a.txt", "new.txt")
        self.assertEqual(self.read("a.txt"), b"two\n")
        self.assertEqual(self.read("new.txt"), b"n\n")
        self.assertStatus([" M a.txt", "?? new.txt"])

    def test_restore_source_worktree_only(self):
        self.write("a.txt", "one\n")
        c1 = self.commit_all("first")
        self.write("a.txt", "two\n")
        c2 = self.commit_all("second")

        self.mg("restore", "--source", "HEAD~1", "a.txt")
        self.assertEqual(self.read("a.txt"), b"one\n")
        self.assertEqual(self.ls_stage()["a.txt"][0][1], blob_sha(b"two\n"), "index must not change")
        self.assertStatus([" M a.txt"])
        self.assertEqual(self.head(), c2)

        self.mg("restore", "-s", c2[:7], "a.txt")
        self.assertEqual(self.read("a.txt"), b"two\n")
        self.assertStatus([])
        self.assertNotEqual(c1, c2)

    def test_restore_source_staged_and_worktree(self):
        self.write("a.txt", "one\n")
        self.commit_all("first")
        self.write("a.txt", "two\n")
        c2 = self.commit_all("second")

        self.mg("restore", "--staged", "--worktree", "--source", "HEAD~1", "a.txt")
        self.assertEqual(self.read("a.txt"), b"one\n")
        self.assertEqual(self.ls_stage()["a.txt"][0][1], blob_sha(b"one\n"))
        self.assertEqual(self.head(), c2)
        self.assertStatus(["M  a.txt"])

    def test_restore_from_source_that_lacks_path_removes_it(self):
        # git restore: "If a path is tracked but does not exist in the restore source,
        # it will be removed to match the source." SPEC: checkout REV -- p == restore with
        # both flags from REV. In no case may the command silently leave the file behind.
        self.write("a.txt", "a\n")
        self.commit_all("first")
        self.write("b.txt", "b\n")
        self.commit_all("second adds b")
        cases = [
            (("restore", "--staged", "--worktree", "--source", "HEAD~1", "b.txt"), ["D  b.txt"]),
            (("restore", "--source", "HEAD~1", "b.txt"), [" D b.txt"]),
            (("checkout", "HEAD~1", "--", "b.txt"), ["D  b.txt"]),
        ]
        for args, expected in cases:
            with self.subTest(cmd=" ".join(args)):
                self.mg("reset", "--hard")
                self.assertStatus([])
                self.mg(*args)
                self.assertFalse(self.exists("b.txt"), "worktree should match the source (path absent)")
                self.assertStatus(expected)

    def test_checkout_dashdash_paths(self):
        self.write("a.txt", "one\n")
        self.commit_all("first")
        self.write("a.txt", "two\n")
        c2 = self.commit_all("second")
        self.write("a.txt", "dirty\n")

        self.mg("checkout", "--", "a.txt")
        self.assertEqual(self.read("a.txt"), b"two\n")
        self.assertStatus([])

        self.mg("checkout", "HEAD~1", "--", "a.txt")
        self.assertEqual(self.read("a.txt"), b"one\n")
        self.assertEqual(self.head(), c2)
        self.assertEqual(self.special("HEAD"), "ref: refs/heads/main", "path checkout must not detach")
        self.assertStatus(["M  a.txt"])

    def test_restore_directory_and_dot_keep_untracked(self):
        self.write("sub/x.txt", "x\n")
        self.write("sub/deep/y.txt", "y\n")
        self.write("z.txt", "z\n")
        self.commit_all("first")
        self.write("sub/x.txt", "changed\n")
        self.delete("sub/deep")
        self.write("z.txt", "changed\n")
        self.write("sub/u.txt", "untracked\n")

        self.mg("restore", "sub")
        self.assertEqual(self.read("sub/x.txt"), b"x\n")
        self.assertEqual(self.read("sub/deep/y.txt"), b"y\n")
        self.assertEqual(self.read("z.txt"), b"changed\n")
        self.assertStatus([" M z.txt", "?? sub/u.txt"])

        self.mg("restore", ".")
        self.assertEqual(self.read("z.txt"), b"z\n")
        self.assertEqual(self.read("sub/u.txt"), b"untracked\n")
        self.assertStatus(["?? sub/u.txt"])

    def test_restore_errors(self):
        self.write("a.txt", "a\n")
        self.commit_all("first")
        self.write("u.txt", "untracked\n")

        r = self.mg("restore", "u.txt", code=1)
        self.assertIn("did not match", r.err)
        self.assertEqual(self.read("u.txt"), b"untracked\n")
        self.mg("restore", "nope.txt", code=1)
        r = self.mg("restore", code=128)
        self.assertEqual(r.err, "fatal: you must specify path(s) to restore\n")
        r = self.mg("restore", "--source", "nosuchrev", "a.txt", code=128)
        self.assertIn("unknown revision", r.err)
        self.assertStatus(["?? u.txt"])

    def test_restore_refuses_unmerged_path(self):
        # git: "error: path 'f.txt' is unmerged" (exit 1); nothing changes.
        self.make_conflict()
        before = self.read("f.txt")
        r = self.mg("restore", "f.txt", code=None)
        self.assertEqual(self.read("f.txt"), before)
        self.assertIsNotNone(self.special("MERGE_HEAD"))
        self.assertNotEqual(r.code, 0, f"restore of an unmerged path should fail, not silently no-op:\n{r}")

    def test_restore_and_hard_reset_preserve_exact_bytes(self):
        crlf = b"line1\r\nline2\r\nno final newline"
        binary = bytes(range(256)) * 4 + b"\x00\x00\xff"
        self.write("crlf.txt", crlf)
        self.write("bin.dat", binary)
        self.commit_all("bytes")

        self.write("crlf.txt", b"junk\n")
        self.write("bin.dat", b"junk")
        self.mg("restore", "crlf.txt", "bin.dat")
        self.assertEqual(self.read("crlf.txt"), crlf)
        self.assertEqual(self.read("bin.dat"), binary)

        self.write("crlf.txt", b"line1\nline2\nno final newline")  # CRLF -> LF only
        self.delete("bin.dat")
        self.mg("reset", "--hard")
        self.assertEqual(self.read("crlf.txt"), crlf)
        self.assertEqual(self.read("bin.dat"), binary)
        self.assertStatus([])


# =========================================================================================
# .minigitignore
# =========================================================================================
class IgnoreTests(MinigitCase):

    def test_glob_matches_basename_at_any_depth(self):
        self.write(".minigitignore", "*.log\n")
        for f in ("a.log", "sub/b.log", "sub/deep/c.log"):
            self.write(f, "ignored\n")
        for f in ("keep.txt", "notes.log.txt", "sub/d.txt"):
            self.write(f, "shown\n")

        self.assertStatus(["?? .minigitignore", "?? keep.txt", "?? notes.log.txt", "?? sub/d.txt"])
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), [".minigitignore", "keep.txt", "notes.log.txt", "sub/d.txt"])
        self.mg("commit", "-m", "c")
        self.assertIn("nothing to commit, working tree clean", self.mg("status").out)

    def test_negation_and_last_match_wins(self):
        self.write(".minigitignore", "*.log\n!keep.log\n")
        self.write("a.log", "x\n")
        self.write("keep.log", "x\n")
        self.write("sub/keep.log", "x\n")
        self.assertStatus(["?? .minigitignore", "?? keep.log", "?? sub/keep.log"])

        self.write(".minigitignore", "!keep.log\n*.log\n")   # order flipped: *.log wins
        self.assertStatus(["?? .minigitignore"])

    def test_dir_only_and_anchoring(self):
        self.write(".minigitignore", "build/\n/top.txt\ndocs/*.md\n")
        self.write("build/out.bin", "x")
        self.write("sub/build/x.o", "x")          # dir-only pattern without '/' matches at any depth
        self.write("other/build", "a file")       # a FILE named build is not matched by build/
        self.write("top.txt", "x")
        self.write("sub/top.txt", "x")            # /top.txt is anchored to the root
        self.write("docs/a.md", "x")
        self.write("docs/sub/b.md", "x")          # '*' does not cross '/'
        self.write("x/docs/c.md", "x")            # inner '/' anchors docs/ to the root
        self.assertStatus(["?? .minigitignore", "?? docs/sub/b.md", "?? other/build",
                           "?? sub/top.txt", "?? x/docs/c.md"])

    def test_double_star_and_wildcards(self):
        self.write(".minigitignore", "**/tmp\na/**/b.txt\nlogs/**\nfile?.txt\n[ab].dat\n")
        ignored = ["tmp/1", "x/tmp/2", "x/y/tmp/3", "a/b.txt", "a/x/b.txt", "a/x/y/b.txt",
                   "logs/l1", "logs/deep/l2", "file1.txt", "a.dat", "b.dat"]
        shown = ["tmpx/4", "c/a/b.txt", "a/b.txt.bak", "logs.txt", "file10.txt", "c.dat"]
        for f in ignored + shown:
            self.write(f, "x")
        self.assertStatus(["?? .minigitignore"] + [f"?? {f}" for f in shown])

    def test_nested_ignore_file_is_relative_to_its_directory(self):
        self.write(".minigitignore", "*.tmp\n")
        self.write("sub/.minigitignore", "/local.txt\n!keep.tmp\ndeep/*.o\n")
        files = {
            "local.txt": True, "sub/local.txt": False, "sub/x/local.txt": True,
            "keep.tmp": False, "sub/keep.tmp": True, "sub/x/keep.tmp": True, "sub/other.tmp": False,
            "sub/deep/a.o": False, "sub/x/deep/b.o": True,
        }
        for f in files:
            self.write(f, "x")
        expected = ["?? .minigitignore", "?? sub/.minigitignore"] + \
                   [f"?? {f}" for f, shown in files.items() if shown]
        self.assertStatus(expected)

    def test_reinclude_inside_excluded_directory(self):
        # git: a file cannot be re-included when its parent directory is excluded ("build/"),
        # but it can be when only the directory's contents are excluded ("out/*").
        self.write(".minigitignore", "build/\n!build/keep.txt\nout/*\n!out/keep.txt\n")
        for f in ("build/keep.txt", "build/x.txt", "out/keep.txt", "out/x.txt"):
            self.write(f, "x")
        self.assertStatus(["?? .minigitignore", "?? out/keep.txt"])
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), [".minigitignore", "out/keep.txt"])

    def test_ignore_file_syntax_comments_escapes_whitespace_crlf(self):
        self.write(".minigitignore",
                   b"# a comment\r\n\r\n   \r\n\\#hash.txt\r\nsecret.txt   \r\n*.bak\r\n!keep.bak\r\n")
        for f in ("# a comment", "#hash.txt", "secret.txt", "x.bak", "keep.bak", "normal.txt"):
            self.write(f, "x")
        self.assertStatus(["?? # a comment", "?? .minigitignore", "?? keep.bak", "?? normal.txt"])

    def test_tracked_files_are_never_ignored(self):
        self.write("a.log", "v1\n")
        self.mg("add", "-f", "a.log")
        self.write(".minigitignore", "*.log\n")
        self.commit_all("track a.log")
        self.assertIn("a.log", self.ls_files())

        self.write("a.log", "v2\n")
        self.assertStatus([" M a.log"])
        self.mg("add", "a.log")                    # no -f needed for a tracked file
        self.assertStatus(["M  a.log"])
        self.mg("commit", "-m", "v2")
        self.write("a.log", "v3\n")
        self.mg("commit", "-a", "-m", "v3")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:a.log").raw, b"v3\n")
        self.assertStatus([])

        self.mg("rm", "--cached", "a.log")         # now untracked AND ignored: no "??" line
        self.assertTrue(self.exists("a.log"))
        self.assertStatus(["D  a.log"])

    def test_add_named_ignored_path_requires_force_and_is_all_or_nothing(self):
        self.write(".minigitignore", "*.secret\nprivate/\n")
        self.write("x.secret", "s\n")
        self.write("private/p.txt", "p\n")
        self.write("ok.txt", "ok\n")

        r = self.mg("add", "x.secret", code=1)
        self.assertEqual(r.err, "The following paths are ignored by one of your .minigitignore files:\n"
                                "x.secret\nhint: Use -f if you really want to add them.\n")
        self.mg("add", "private/p.txt", code=1)
        self.mg("add", "ok.txt", "x.secret", code=1)
        self.assertEqual(self.ls_files(), [], "a failed add must not stage anything")

        self.mg("add", "-f", "x.secret")
        self.mg("add", "-f", "private/p.txt")
        self.assertEqual(self.ls_files(), ["private/p.txt", "x.secret"])

    def test_add_force_on_ignored_directory_stages_its_files(self):
        # git: "add -f <ignored dir>" and "add -f ." stage the ignored files they cover.
        self.write(".minigitignore", "private/\n*.log\n")
        self.write("private/a.txt", "a\n")
        self.write("private/deep/b.txt", "b\n")
        self.write("x.log", "l\n")
        r = self.mg("add", "private", code=1)
        self.assertIn("ignored", r.err)

        with self.subTest(cmd="add -f private"):
            self.mg("add", "-f", "private")
            self.assertEqual(self.ls_files(), ["private/a.txt", "private/deep/b.txt"])
        with self.subTest(cmd="add -f ."):
            self.mg("add", "-f", ".")
            self.assertEqual(self.ls_files(), [".minigitignore", "private/a.txt",
                                               "private/deep/b.txt", "x.log"])

    def test_minigit_dir_is_always_ignored(self):
        self.write(".minigitignore", "!.minigit\n!.minigit/\n!.minigit/**\n")
        self.write("a.txt", "a\n")
        self.assertStatus(["?? .minigitignore", "?? a.txt"])
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), [".minigitignore", "a.txt"])
        r = self.mg("add", "-f", ".minigit/config", code=None)
        self.assertNotEqual(r.code, 0, f"staging a file inside .minigit must be refused:\n{r}")
        self.assertEqual(self.ls_files(), [".minigitignore", "a.txt"])

    def test_ignored_directory_with_tracked_file(self):
        self.write("lib/a.txt", "a\n")
        self.commit_all("lib")
        self.write(".minigitignore", "/lib/\n")
        self.commit_all("ignore lib")
        self.write("lib/b.txt", "untracked in ignored dir\n")
        self.write("lib/a.txt", "changed\n")

        self.assertStatus([" M lib/a.txt"])
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), [".minigitignore", "lib/a.txt"])
        self.assertStatus(["M  lib/a.txt"])


if __name__ == "__main__":
    unittest.main()

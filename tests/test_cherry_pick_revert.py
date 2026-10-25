"""Acceptance tests for ``minigit cherry-pick`` and ``minigit revert`` (single commits).

Design: features/cherry-pick-revert.md. These tests drive the real CLI (``python -m minigit``)
as a subprocess inside temporary repositories and import nothing from ``minigit`` or ``tests/``.

"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 60

BASE = b"a\nb\nc\n"
SUMMARY_RE = r"^\[main [0-9a-f]{7}\] "


# ---------------------------------------------------------------------------
# helpers (self-contained copy of the tests_blackbox style)
# ---------------------------------------------------------------------------

@dataclass
class Result:
    args: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    @property
    def text(self) -> str:
        return self.out + self.err

    def __str__(self) -> str:
        return (f"minigit {' '.join(self.args)} -> exit {self.returncode}\n"
                f"--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}")


def make_env(tmp: str) -> dict:
    env = dict(os.environ)
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(home, ".minigitconfig")
    env["MINIGIT_CREDENTIALS"] = os.path.join(home, ".minigit-credentials.json")
    env["MINIGIT_AUTHOR_NAME"] = "QA Tester"
    env["MINIGIT_AUTHOR_EMAIL"] = "qa@example.com"
    env["MINIGIT_AUTHOR_DATE"] = "1700000000 +0000"
    env["PYTHONPATH"] = ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    for key in ("MINIGIT_DEBUG", "MINIGIT_USER", "MINIGIT_TOKEN"):
        env.pop(key, None)
    return env


FEAT_AUTHOR = ("Feat Author", "feat@example.com", "1600000000 +0100")


class Repo:
    """One working repository driven through the CLI."""

    def __init__(self, path: str, env: dict) -> None:
        self.path = path
        self.env = env
        os.makedirs(path, exist_ok=True)

    def run(self, *args: str, check: bool = True, env: dict | None = None) -> Result:
        argv = [str(a) for a in args]
        proc = subprocess.run([sys.executable, "-m", "minigit", *argv], cwd=self.path,
                              env=env or self.env, capture_output=True, timeout=TIMEOUT)
        res = Result(argv, proc.returncode, proc.stdout, proc.stderr)
        if check and res.returncode != 0:
            raise AssertionError(f"command failed:\n{res}")
        return res

    def abs(self, rel: str) -> str:
        return os.path.join(self.path, *rel.split("/"))

    def write(self, rel: str, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        p = self.abs(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)

    def read(self, rel: str) -> bytes:
        with open(self.abs(rel), "rb") as fh:
            return fh.read()

    def exists(self, rel: str) -> bool:
        return os.path.lexists(self.abs(rel))

    def remove(self, rel: str) -> None:
        os.unlink(self.abs(rel))

    def state(self, name: str) -> str | None:
        """Content of .minigit/<name> (stripped), or None when absent."""
        p = os.path.join(self.path, ".minigit", name)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as fh:
            return fh.read().decode("utf-8", "replace").strip()

    def write_gitfile(self, name: str, data: str) -> None:
        with open(os.path.join(self.path, ".minigit", *name.split("/")), "wb") as fh:
            fh.write(data.encode("utf-8"))

    def sha(self, rev: str = "HEAD") -> str:
        return self.run("rev-parse", rev).out.strip()

    def commit_text(self, rev: str = "HEAD") -> str:
        return self.run("cat-file", "-p", rev).out

    def header(self, rev: str, key: str) -> list[str]:
        """Values of ``key`` lines ("tree", "parent", "author", "committer") of a commit."""
        head = self.commit_text(rev).split("\n\n", 1)[0]
        return [ln[len(key) + 1:] for ln in head.splitlines() if ln.startswith(key + " ")]

    def parents(self, rev: str = "HEAD") -> list[str]:
        return self.header(rev, "parent")

    def tree(self, rev: str = "HEAD") -> str:
        return self.header(rev, "tree")[0]

    def message(self, rev: str = "HEAD") -> str:
        return self.commit_text(rev).split("\n\n", 1)[1]

    def porcelain(self) -> list[str]:
        return [ln for ln in self.run("status", "--porcelain").out.splitlines() if ln]

    def ls_files(self) -> list[str]:
        return sorted(self.run("ls-files").out.split())

    def stages_of(self, path: str) -> set[int]:
        out = set()
        for ln in self.run("ls-files", "-s").out.splitlines():
            if not ln.strip():
                continue
            meta, _, p = ln.partition("\t")
            if p == path:
                out.add(int(meta.split()[2]))
        return out

    def commit_all(self, msg: str, env: dict | None = None) -> str:
        self.run("add", ".", env=env)
        self.run("commit", "-m", msg, env=env)
        return self.sha()


class SequencerCase(unittest.TestCase):
    """Common fixture: temp dir, isolated env, fresh repo with a root commit."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tmp.name
        self.env = make_env(self.tmp)
        self.feat_env = dict(self.env, MINIGIT_AUTHOR_NAME=FEAT_AUTHOR[0],
                             MINIGIT_AUTHOR_EMAIL=FEAT_AUTHOR[1], MINIGIT_AUTHOR_DATE=FEAT_AUTHOR[2])
        self.repo = Repo(os.path.join(self.tmp, "work"), self.env)
        self.repo.run("init")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def seed(self) -> str:
        """Root commit: f.txt = BASE, other.txt."""
        self.repo.write("f.txt", BASE)
        self.repo.write("other.txt", b"other\n")
        return self.repo.commit_all("base")

    def feat_commit(self, files: dict[str, bytes | None], msg: str = "feat change") -> str:
        """On branch 'feat' (created at HEAD if missing), commit ``files`` as FEAT_AUTHOR; HEAD back on main.

        A ``None`` value deletes that path. Returns the feat commit sha.
        """
        r = self.repo
        if r.run("branch").out.find(" feat") < 0:
            r.run("switch", "-c", "feat")
        else:
            r.run("switch", "feat")
        for p, data in files.items():
            if data is None:
                r.run("rm", p)
            else:
                r.write(p, data)
        sha = r.commit_all(msg, env=self.feat_env)
        r.run("switch", "main")
        return sha

    def conflicted_pick(self) -> tuple[str, str, Result]:
        """main and feat edit the same line of f.txt; cherry-pick feat conflicts.

        Returns (main_sha, feat_sha, result).
        """
        r = self.repo
        self.seed()
        feat = self.feat_commit({"f.txt": b"a\nb\nC-feat\n"})
        r.write("f.txt", b"a\nb\nC-main\n")
        main = r.commit_all("main work")
        res = r.run("cherry-pick", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        return main, feat, res


# ---------------------------------------------------------------------------
# cherry-pick
# ---------------------------------------------------------------------------

class CherryPickCleanTests(SequencerCase):

    def test_clean_pick_creates_single_parent_commit_with_original_message_and_author(self):
        r = self.repo
        self.seed()
        feat = self.feat_commit({"new.txt": b"from feat\n"})
        r.write("f.txt", b"a\nb\nc\nd-main\n")
        main = r.commit_all("main work")

        res = r.run("cherry-pick", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), SUMMARY_RE + "feat change$")
        self.assertEqual(res.err, "", res)
        head = r.sha("HEAD")
        self.assertNotEqual(head, feat, "a new commit object must be created, not the branch tip reused")
        self.assertEqual(r.parents(head), [main], "exactly one parent: the old HEAD")
        self.assertEqual(r.message(head), "feat change\n")
        self.assertEqual(r.header(head, "author"), [f"{FEAT_AUTHOR[0]} <{FEAT_AUTHOR[1]}> {FEAT_AUTHOR[2]}"],
                         "the original author (name, email, date) is preserved")
        self.assertEqual(r.header(head, "committer"), ["QA Tester <qa@example.com> 1700000000 +0000"],
                         "the committer is the current identity")
        self.assertEqual(r.read("new.txt"), b"from feat\n")
        self.assertEqual(r.read("f.txt"), b"a\nb\nc\nd-main\n", "HEAD's own changes stay")
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.sha("main"), head)
        self.assertEqual(r.sha("feat"), feat, "the picked branch does not move")
        self.assertEqual(r.state("ORIG_HEAD"), main)
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.run("fsck").out.strip(), "")
        # log shows a linear history: main work -> feat change
        self.assertEqual([ln.split(" ", 1)[1].split(") ")[-1] for ln in r.run("log", "--oneline").out.splitlines()],
                         ["feat change", "main work", "base"])
        # show prints the delta against the single parent
        show = r.run("show", "HEAD").out
        self.assertIn("diff --git a/new.txt b/new.txt", show)
        self.assertNotIn("Merge:", show)

    def test_pick_by_sha_with_x_records_origin_line(self):
        r = self.repo
        self.seed()
        feat = self.feat_commit({"new.txt": b"n\n"}, msg="Add new\n\nWith a body.")
        r.write("f.txt", b"a\nb\nc\nd\n")
        r.commit_all("main work")
        res = r.run("cherry-pick", "-x", feat)
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.message("HEAD"), f"Add new\n\nWith a body.\n\n(cherry picked from commit {feat})\n")
        self.assertRegex(res.out.strip(), SUMMARY_RE + "Add new$")

    def test_pick_applies_only_that_commits_delta_and_merges_with_head_edits(self):
        r = self.repo
        r.write("f.txt", b"1\n2\n3\n4\n5\n")
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.write("x.txt", b"x\n")
        c1 = r.commit_all("c1 adds x", env=self.feat_env)
        r.write("f.txt", b"1\n2\n3\n4\nFIVE\n")
        r.write("y.txt", b"y\n")
        c2 = r.commit_all("c2 edits end and adds y", env=self.feat_env)
        r.run("switch", "main")
        r.write("f.txt", b"ONE\n2\n3\n4\n5\n")
        main = r.commit_all("main edits start")

        res = r.run("cherry-pick", c2)
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.read("f.txt"), b"ONE\n2\n3\n4\nFIVE\n", "three-way: base is c2's parent")
        self.assertEqual(r.read("y.txt"), b"y\n")
        self.assertFalse(r.exists("x.txt"), "c1 is not part of c2's delta")
        self.assertEqual(r.ls_files(), ["f.txt", "y.txt"])
        self.assertEqual(r.parents(), [main])
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.sha("feat"), c2)
        self.assertNotEqual(r.sha("HEAD"), c1)

    def test_pick_of_a_deletion_removes_file_and_prunes_empty_dirs(self):
        r = self.repo
        r.write("f.txt", BASE)
        r.write("dir/sub/gone.txt", b"gone\n")
        r.commit_all("base")
        self.feat_commit({"dir/sub/gone.txt": None}, msg="remove gone")
        r.write("f.txt", b"a\nb\nc\nd\n")
        r.commit_all("main work")
        self.assertTrue(r.exists("dir/sub/gone.txt"))

        res = r.run("cherry-pick", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertFalse(r.exists("dir/sub/gone.txt"))
        self.assertFalse(r.exists("dir"), "empty directories are pruned")
        self.assertEqual(r.ls_files(), ["f.txt"])
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.message(), "remove gone\n")

    def test_pick_on_detached_head_does_not_move_branches(self):
        r = self.repo
        base = self.seed()
        feat = self.feat_commit({"new.txt": b"n\n"})
        r.write("f.txt", b"a\nb\nc\nd\n")
        main = r.commit_all("main work")
        r.run("checkout", base)
        self.assertIn("HEAD detached", r.run("status").out)

        res = r.run("cherry-pick", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), r"^\[detached HEAD [0-9a-f]{7}\] feat change$")
        head = r.sha("HEAD")
        self.assertEqual(r.parents(head), [base])
        self.assertEqual(r.read("new.txt"), b"n\n")
        self.assertEqual(r.read("f.txt"), BASE)
        self.assertEqual(r.sha("main"), main)
        self.assertEqual(r.sha("feat"), feat)
        self.assertIn(f"HEAD detached at {head[:7]}", r.run("status").out)

    def test_pick_root_commit_uses_empty_tree_as_base(self):
        r = self.repo
        base = self.seed()
        # make an unborn branch 'orphan' by hand (like a fresh init), then a root commit on it
        r.write_gitfile("HEAD", "ref: refs/heads/orphan\n")
        os.unlink(os.path.join(r.path, ".minigit", "index"))
        r.remove("f.txt")
        r.remove("other.txt")
        self.assertIn("No commits yet", r.run("status").out)
        r.write("r.txt", b"root file\n")
        root = r.commit_all("orphan root")
        self.assertEqual(r.parents(root), [])
        r.run("switch", "main")
        self.assertEqual(r.sha("HEAD"), base)
        self.assertFalse(r.exists("r.txt"))

        res = r.run("cherry-pick", "orphan")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.read("r.txt"), b"root file\n")
        self.assertEqual(r.read("f.txt"), BASE)
        self.assertEqual(r.parents(), [base])
        self.assertEqual(r.message(), "orphan root\n")
        self.assertEqual(r.ls_files(), ["f.txt", "other.txt", "r.txt"])

    def test_pick_carries_unrelated_dirty_and_untracked_files(self):
        r = self.repo
        self.seed()
        self.feat_commit({"new.txt": b"n\n"})
        r.write("f.txt", b"a\nb\nc\nd\n")
        r.commit_all("main work")
        r.write("other.txt", b"dirty other\n")
        r.write("untracked.txt", b"keep me\n")

        res = r.run("cherry-pick", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.read("other.txt"), b"dirty other\n")
        self.assertEqual(r.read("untracked.txt"), b"keep me\n")
        self.assertEqual(r.run("cat-file", "-p", "HEAD:other.txt").stdout, b"other\n",
                         "the commit is built from the index, not the dirty worktree")
        self.assertEqual(sorted(r.porcelain()), [" M other.txt", "?? untracked.txt"])


class CherryPickEmptyAndMainlineTests(SequencerCase):

    def test_pick_with_no_effect_is_refused_unless_allow_empty(self):
        r = self.repo
        self.seed()
        r.write("f.txt", b"a\nb\nc\nd\n")
        head = r.commit_all("second")
        res = r.run("cherry-pick", "HEAD", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"cherry-pick of {head[:7]} produced no changes", res.err, res)
        self.assertIn("--allow-empty", res.err, res)
        self.assertEqual(r.sha("HEAD"), head, "nothing was committed")
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        self.assertEqual(r.porcelain(), [])

        # the same change made independently on another branch is also "already applied"
        r.run("switch", "-c", "feat", "HEAD~1")
        r.write("f.txt", b"a\nb\nc\nd\n")
        feat = r.commit_all("same change elsewhere")
        r.run("switch", "main")
        res = r.run("cherry-pick", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"cherry-pick of {feat[:7]} produced no changes", res.err, res)
        self.assertEqual(r.sha("HEAD"), head)

        res = r.run("cherry-pick", "--allow-empty", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), SUMMARY_RE + "same change elsewhere$")
        new = r.sha("HEAD")
        self.assertEqual(r.parents(new), [head])
        self.assertEqual(r.tree(new), r.tree(head), "an empty commit reuses HEAD's tree")
        self.assertEqual(r.message(new), "same change elsewhere\n")

    def test_merge_commit_needs_mainline_and_mainline_selects_the_parent(self):
        r = self.repo
        base = self.seed()
        feat = self.feat_commit({"f-side.txt": b"f\n"}, msg="feat adds f-side")
        r.write("m-side.txt", b"m\n")
        mn = r.commit_all("main adds m-side")
        r.run("merge", "feat")
        merge = r.sha("HEAD")
        self.assertEqual(r.parents(merge), [mn, feat])

        r.run("switch", "-c", "pick1", base)
        res = r.run("cherry-pick", merge, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"commit {merge[:7]} is a merge but no -m option was given", res.err, res)
        self.assertEqual(r.sha("HEAD"), base)
        self.assertEqual(r.porcelain(), [])

        res = r.run("cherry-pick", "-m", "3", merge, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"commit {merge[:7]} does not have parent 3", res.err, res)

        res = r.run("cherry-pick", "-m", "1", feat, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"mainline was specified but commit {feat[:7]} is not a merge", res.err, res)

        # -m 1: the delta relative to the first parent is what feat brought in
        res = r.run("cherry-pick", "-m", "1", merge)
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.ls_files(), ["f-side.txt", "f.txt", "other.txt"])
        self.assertEqual(r.message(), "Merge branch 'feat'\n")
        self.assertEqual(r.parents(), [base])

        # -m 2: relative to the second parent, the merge brought in main's file
        r.run("switch", "-c", "pick2", base)
        res = r.run("cherry-pick", "--mainline", "2", merge)
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.ls_files(), ["f.txt", "m-side.txt", "other.txt"])
        self.assertEqual(r.parents(), [base])

    def test_unknown_revision_and_non_commit_are_refused(self):
        r = self.repo
        self.seed()
        res = r.run("cherry-pick", "nope", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("unknown revision 'nope'", res.err, res)
        res = r.run("cherry-pick", "HEAD:f.txt", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("'HEAD:f.txt' is not a commit", res.err, res)

    def test_usage_errors_exit_2(self):
        r = self.repo
        self.seed()
        for args in (["cherry-pick"], ["cherry-pick", "--abort", "HEAD"], ["cherry-pick", "--continue", "HEAD"],
                     ["cherry-pick", "--continue", "--abort"], ["revert"], ["revert", "--abort", "HEAD"],
                     ["cherry-pick", "-m", "x", "HEAD"]):
            res = r.run(*args, check=False)
            self.assertEqual(res.returncode, 2, res)
        # --help works and names the options
        helptext = r.run("cherry-pick", "--help").out
        for opt in ("--no-commit", "--continue", "--abort", "--mainline", "--allow-empty", "-x"):
            self.assertIn(opt, helptext)
        helptext = r.run("revert", "--help").out
        for opt in ("--no-commit", "--continue", "--abort", "--mainline"):
            self.assertIn(opt, helptext)
        self.assertIn("cherry-pick", r.run("help").out)
        self.assertIn("revert", r.run("help").out)


class CherryPickConflictTests(SequencerCase):

    def test_conflict_writes_markers_state_message_and_status(self):
        r = self.repo
        main, feat, res = self.conflicted_pick()
        self.assertIn("CONFLICT (content): Merge conflict in f.txt", res.out, res)
        self.assertIn(f"error: could not apply {feat[:7]}... feat change", res.err, res)
        self.assertIn("cherry-pick --continue", res.err, res)
        self.assertIn("cherry-pick --abort", res.err, res)
        self.assertEqual(r.read("f.txt"),
                         b"a\nb\n<<<<<<< HEAD\nC-main\n=======\nC-feat\n>>>>>>> " + feat[:7].encode()
                         + b" (feat change)\n")
        self.assertEqual(r.read("other.txt"), b"other\n")
        self.assertEqual(r.sha("HEAD"), main, "HEAD does not move on conflict")
        self.assertEqual(r.sha("main"), main)
        self.assertEqual(r.state("CHERRY_PICK_HEAD"), feat)
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertIsNone(r.state("REVERT_HEAD"))
        self.assertEqual(r.state("ORIG_HEAD"), main)
        msg = r.state("MERGE_MSG") or ""
        self.assertTrue(msg.startswith("feat change"), msg)
        self.assertIn("# Conflicts:", msg)
        self.assertIn("#\tf.txt", msg)
        self.assertEqual(r.stages_of("f.txt"), {1, 2, 3})
        self.assertEqual(r.stages_of("other.txt"), {0})
        self.assertEqual(r.porcelain(), ["UU f.txt"])
        long = r.run("status").out
        self.assertIn("On branch main", long)
        self.assertIn(f"You are currently cherry-picking commit {feat[:7]}.", long)
        self.assertIn('(fix conflicts and run "minigit cherry-pick --continue")', long)
        self.assertIn("Unmerged paths:", long)
        self.assertIn("\tboth modified:   f.txt", long)
        self.assertNotIn("nothing to commit", long)
        self.assertIn("* Unmerged path f.txt", r.run("diff").out)
        # CHERRY_PICK_HEAD is a usable revision while the pick is open
        self.assertEqual(r.sha("CHERRY_PICK_HEAD"), feat)
        self.assertEqual(r.run("cat-file", "-p", "CHERRY_PICK_HEAD:f.txt").stdout, b"a\nb\nC-feat\n")
        self.assertEqual(r.run("log", "--oneline", "-n", "1", "CHERRY_PICK_HEAD").out.split()[0], feat[:7])

    def test_continue_after_resolving_keeps_original_author_and_message(self):
        r = self.repo
        main, feat, _ = self.conflicted_pick()
        res = r.run("cherry-pick", "--continue", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("cannot commit: unresolved conflicts in: f.txt", res.err, res)
        self.assertEqual(r.state("CHERRY_PICK_HEAD"), feat, "a refused --continue keeps the state")

        r.write("f.txt", b"a\nb\nC-both\n")
        r.run("add", "f.txt")
        self.assertEqual(r.stages_of("f.txt"), {0})
        long = r.run("status").out
        self.assertIn(f"You are currently cherry-picking commit {feat[:7]}.", long)
        self.assertIn('(all conflicts fixed: run "minigit cherry-pick --continue")', long)
        self.assertNotIn("Unmerged paths", long)

        res = r.run("cherry-pick", "--continue")
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), SUMMARY_RE + "feat change$")
        head = r.sha("HEAD")
        self.assertEqual(r.parents(head), [main], "single parent, never CHERRY_PICK_HEAD")
        self.assertEqual(r.message(head), "feat change\n", "'# Conflicts:' lines are dropped")
        self.assertEqual(r.header(head, "author"), [f"{FEAT_AUTHOR[0]} <{FEAT_AUTHOR[1]}> {FEAT_AUTHOR[2]}"])
        self.assertEqual(r.header(head, "committer"), ["QA Tester <qa@example.com> 1700000000 +0000"])
        self.assertEqual(r.read("f.txt"), b"a\nb\nC-both\n")
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.porcelain(), [])
        self.assertNotIn("cherry-picking", r.run("status").out)
        self.assertEqual(r.run("fsck").out.strip(), "")
        res = r.run("cherry-pick", "--continue", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("no cherry-pick in progress (CHERRY_PICK_HEAD missing)", res.err, res)

    def test_plain_commit_finishes_a_conflicted_pick(self):
        r = self.repo
        main, feat, _ = self.conflicted_pick()
        r.run("checkout", "CHERRY_PICK_HEAD", "--", "f.txt")   # take theirs
        self.assertEqual(r.read("f.txt"), b"a\nb\nC-feat\n")
        self.assertEqual(r.porcelain(), ["M  f.txt"])
        res = r.run("commit")                                   # no -m: MERGE_MSG is used
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), SUMMARY_RE + "feat change$")
        self.assertEqual(r.message(), "feat change\n")
        self.assertEqual(r.parents(), [main])
        self.assertEqual(r.header("HEAD", "author"), [f"{FEAT_AUTHOR[0]} <{FEAT_AUTHOR[1]}> {FEAT_AUTHOR[2]}"])
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))

        # a custom -m during an open pick is allowed and also clears the state
        # (main edits line 3 again first, otherwise feat2's delta C-feat -> C-feat-2 would apply cleanly)
        feat2 = self.feat_commit({"f.txt": b"a\nb\nC-feat-2\n"}, msg="feat again")
        r.write("f.txt", b"a\nb\nC-main-2\n")
        r.commit_all("main again")
        res = r.run("cherry-pick", feat2, check=False)
        self.assertEqual(res.returncode, 1, res)
        r.write("f.txt", b"a\nb\nresolved\n")
        r.run("add", "f.txt")
        res = r.run("commit", "-m", "custom resolution")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.message(), "custom resolution\n")
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))

    def test_abort_restores_head_state_and_keeps_unrelated_dirty_file(self):
        r = self.repo
        self.seed()
        feat = self.feat_commit({"f.txt": b"a\nb\nC-feat\n", "added.txt": b"added by feat\n"})
        r.write("f.txt", b"a\nb\nC-main\n")
        main = r.commit_all("main work")
        r.write("other.txt", b"dirty other\n")      # unrelated, unstaged
        r.write("untracked.txt", b"u\n")

        res = r.run("cherry-pick", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(r.read("added.txt"), b"added by feat\n", "clean parts of the pick are applied")
        self.assertEqual(sorted(r.porcelain()), [" M other.txt", "?? untracked.txt", "A  added.txt", "UU f.txt"])

        res = r.run("cherry-pick", "--abort")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(res.out, "", res)
        self.assertEqual(r.read("f.txt"), b"a\nb\nC-main\n")
        self.assertFalse(r.exists("added.txt"))
        self.assertEqual(r.stages_of("f.txt"), {0})
        self.assertEqual(r.ls_files(), ["f.txt", "other.txt"])
        self.assertEqual(r.read("other.txt"), b"dirty other\n", "unrelated local changes survive")
        self.assertEqual(r.read("untracked.txt"), b"u\n")
        self.assertEqual(sorted(r.porcelain()), [" M other.txt", "?? untracked.txt"])
        self.assertEqual(r.sha("HEAD"), main)
        self.assertEqual(r.sha("feat"), feat)
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertNotIn("cherry-picking", r.run("status").out)
        res = r.run("cherry-pick", "--abort", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("there is no cherry-pick to abort (CHERRY_PICK_HEAD missing)", res.err, res)

    def test_other_commands_refuse_during_an_open_pick_and_reset_clears_it(self):
        r = self.repo
        main, feat, _ = self.conflicted_pick()
        r.run("branch", "elsewhere", main)

        res = r.run("cherry-pick", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have not concluded your cherry-pick (CHERRY_PICK_HEAD exists)", res.err, res)
        res = r.run("revert", "HEAD", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have not concluded your cherry-pick (CHERRY_PICK_HEAD exists)", res.err, res)
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have not concluded your cherry-pick (CHERRY_PICK_HEAD exists)", res.err, res)
        res = r.run("switch", "elsewhere", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("cherry-pick in progress", res.err, res)
        res = r.run("checkout", "elsewhere", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("cherry-pick in progress", res.err, res)
        res = r.run("reset", "--soft", "HEAD", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("cannot do a soft reset in the middle of a cherry-pick", res.err, res)
        res = r.run("commit", "-m", "try", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("cannot commit: unresolved conflicts in: f.txt", res.err, res)
        # every refusal kept the state intact
        self.assertEqual(r.state("CHERRY_PICK_HEAD"), feat)
        self.assertEqual(r.stages_of("f.txt"), {1, 2, 3})
        self.assertEqual(r.sha("HEAD"), main)

        res = r.run("reset", "--hard")
        self.assertEqual(res.returncode, 0, res)
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.read("f.txt"), b"a\nb\nC-main\n")
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.sha("HEAD"), main)
        # and a plain (mixed) reset clears the state too
        self.conflicted_pick_again_then_mixed_reset(main, feat)

    def conflicted_pick_again_then_mixed_reset(self, main: str, feat: str) -> None:
        r = self.repo
        res = r.run("cherry-pick", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(r.state("CHERRY_PICK_HEAD"), feat)
        r.run("reset")
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        self.assertEqual(r.stages_of("f.txt"), {0})
        self.assertEqual(r.porcelain(), [" M f.txt"], "mixed reset leaves the marker file in the worktree")
        self.assertEqual(r.sha("HEAD"), main)

    def test_pick_refuses_staged_changes_dirty_affected_file_and_unborn_head(self):
        r = self.repo
        self.seed()
        feat = self.feat_commit({"f.txt": b"a\nb\nC-feat\n"})
        r.write("f.txt", b"a\nb\nc\nd\n")
        main = r.commit_all("main work")

        r.write("other.txt", b"staged\n")
        r.run("add", "other.txt")
        res = r.run("cherry-pick", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have staged changes; commit them first", res.err, res)
        self.assertEqual(r.sha("HEAD"), main)
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        r.run("restore", "--staged", "other.txt")
        r.run("restore", "other.txt")

        r.write("f.txt", b"dirty local edit\n")
        res = r.run("cherry-pick", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("would be overwritten by cherry-pick", res.err, res)
        self.assertIn("\tf.txt", res.err, res)
        self.assertEqual(r.read("f.txt"), b"dirty local edit\n", "nothing is touched")
        self.assertEqual(r.sha("HEAD"), main)
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.stages_of("f.txt"), {0})
        r.run("restore", "f.txt")

        # untracked file in the way of a path the pick adds
        feat2 = self.feat_commit({"new.txt": b"from feat\n"}, msg="adds new")
        r.write("new.txt", b"my own\n")
        res = r.run("cherry-pick", feat2, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("would be overwritten by cherry-pick", res.err, res)
        self.assertEqual(r.read("new.txt"), b"my own\n")
        r.remove("new.txt")

        # unborn HEAD
        r.write_gitfile("HEAD", "ref: refs/heads/orphan\n")
        os.unlink(os.path.join(r.path, ".minigit", "index"))
        r.remove("f.txt")
        r.remove("other.txt")
        res = r.run("cherry-pick", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("cannot cherry-pick onto an unborn branch (no commits yet)", res.err, res)
        self.assertFalse(r.exists("f.txt"))
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        self.assertEqual(r.sha("feat"), feat2, "feat now points at feat2 and did not move")
        self.assertNotEqual(feat2, feat)

    def test_modify_delete_conflict_uses_pick_label(self):
        r = self.repo
        self.seed()
        feat = self.feat_commit({"f.txt": b"a\nb\nC-feat\n"})
        r.run("rm", "f.txt")
        r.run("commit", "-m", "main deletes f")
        main = r.sha()
        res = r.run("cherry-pick", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"CONFLICT (modify/delete): f.txt deleted in HEAD and modified in {feat[:7]} (feat change).",
                      res.out, res)
        self.assertEqual(r.read("f.txt"), b"a\nb\nC-feat\n", "the surviving side is written back")
        self.assertEqual(r.stages_of("f.txt"), {1, 3})
        self.assertEqual(r.porcelain(), ["DU f.txt"])
        self.assertIn("\tdeleted by us:   f.txt", r.run("status").out)
        self.assertEqual(r.state("CHERRY_PICK_HEAD"), feat)
        r.run("add", "f.txt")                     # resolve by keeping the picked content
        r.run("cherry-pick", "--continue")
        self.assertEqual(r.parents(), [main])
        self.assertEqual(r.read("f.txt"), b"a\nb\nC-feat\n")
        self.assertEqual(r.porcelain(), [])

    def test_no_commit_applies_to_index_without_state_and_commit_uses_prepared_message(self):
        r = self.repo
        self.seed()
        feat = self.feat_commit({"new.txt": b"n\n", "f.txt": b"a\nb\nc\nfeat\n"})
        # made up front: switching to feat and back after the -n pick would (as in git) drop the
        # staged result, because those paths equal feat's tree
        feat2 = self.feat_commit({"second.txt": b"2\n"}, msg="feat second")
        r.write("other.txt", b"other2\n")
        main = r.commit_all("main work")

        res = r.run("cherry-pick", "-n", feat)
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(res.out, "", "no commit, no summary")
        self.assertEqual(r.sha("HEAD"), main)
        self.assertEqual(sorted(r.porcelain()), ["A  new.txt", "M  f.txt"])
        self.assertEqual(r.read("f.txt"), b"a\nb\nc\nfeat\n")
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"), "a clean --no-commit leaves no in-progress state")
        self.assertNotIn("cherry-picking", r.run("status").out)
        self.assertEqual(r.state("MERGE_MSG"), "feat change")
        # a second -n pick is refused because the index differs from HEAD
        res = r.run("cherry-pick", "-n", feat2, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have staged changes; commit them first", res.err, res)
        # commit without -m takes the prepared message; the author is the current identity
        res = r.run("commit")
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), SUMMARY_RE + "feat change$")
        self.assertEqual(r.message(), "feat change\n")
        self.assertEqual(r.parents(), [main])
        self.assertEqual(r.header("HEAD", "author"), ["QA Tester <qa@example.com> 1700000000 +0000"])
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.sha("feat"), feat2)
        self.assertNotEqual(r.sha("HEAD"), feat)

        # -n with a conflict still records the state so --continue/--abort work
        # (main edits line 4 again first, otherwise feat3's delta feat -> feat-3 would apply cleanly)
        feat3 = self.feat_commit({"f.txt": b"a\nb\nc\nfeat-3\n"}, msg="feat third")
        r.write("f.txt", b"a\nb\nc\nmain-3\n")
        r.commit_all("main edits line 4")
        res = r.run("cherry-pick", "--no-commit", feat3, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (content): Merge conflict in f.txt", res.out, res)
        self.assertEqual(r.state("CHERRY_PICK_HEAD"), feat3)
        r.run("cherry-pick", "--abort")
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.read("f.txt"), b"a\nb\nc\nmain-3\n")


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------

class RevertTests(SequencerCase):

    def three_commits(self) -> tuple[str, str, str]:
        """c1: f.txt=1..5, other; c2: line 1 + adds n.txt; c3: line 5. Returns (c1, c2, c3)."""
        r = self.repo
        r.write("f.txt", b"1\n2\n3\n4\n5\n")
        r.write("other.txt", b"other\n")
        c1 = r.commit_all("c1")
        r.write("f.txt", b"ONE\n2\n3\n4\n5\n")
        r.write("n.txt", b"n\n")
        c2 = r.commit_all("c2 edits line one and adds n")
        r.write("f.txt", b"ONE\n2\n3\n4\nFIVE\n")
        c3 = r.commit_all("c3 edits line five")
        return c1, c2, c3

    def test_revert_head_creates_inverse_commit(self):
        r = self.repo
        c1, c2, c3 = self.three_commits()
        res = r.run("revert", "HEAD")
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), SUMMARY_RE + 'Revert "c3 edits line five"$')
        self.assertEqual(res.err, "", res)
        head = r.sha("HEAD")
        self.assertEqual(r.parents(head), [c3])
        self.assertEqual(r.message(head), f'Revert "c3 edits line five"\n\nThis reverts commit {c3}.\n')
        self.assertEqual(r.header(head, "author"), ["QA Tester <qa@example.com> 1700000000 +0000"])
        self.assertEqual(r.header(head, "committer"), ["QA Tester <qa@example.com> 1700000000 +0000"])
        self.assertEqual(r.read("f.txt"), b"ONE\n2\n3\n4\n5\n")
        self.assertEqual(r.tree(head), r.tree(c2), "reverting HEAD restores the parent's tree exactly")
        self.assertEqual(r.read("n.txt"), b"n\n")
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.state("ORIG_HEAD"), c3)
        self.assertIsNone(r.state("REVERT_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.run("fsck").out.strip(), "")
        self.assertNotIn("Merge:", r.run("show", "HEAD").out)
        self.assertEqual(r.sha("main"), head)
        self.assertEqual(r.sha("ORIG_HEAD"), c3)

    def test_revert_older_commit_keeps_later_changes_and_deletes_added_file(self):
        r = self.repo
        c1, c2, c3 = self.three_commits()
        res = r.run("revert", c2)
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), SUMMARY_RE + 'Revert "c2 edits line one and adds n"$')
        self.assertEqual(r.read("f.txt"), b"1\n2\n3\n4\nFIVE\n", "line one undone, c3's line five kept")
        self.assertFalse(r.exists("n.txt"), "a file added by the reverted commit is deleted")
        self.assertEqual(r.ls_files(), ["f.txt", "other.txt"])
        self.assertEqual(r.parents(), [c3])
        self.assertEqual(r.message(), f'Revert "c2 edits line one and adds n"\n\nThis reverts commit {c2}.\n')
        self.assertEqual(r.porcelain(), [])
        # reverting it again has nothing to undo
        res = r.run("revert", c2, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"revert of {c2[:7]} produced no changes", res.err, res)
        self.assertIsNone(r.state("REVERT_HEAD"))
        self.assertEqual(r.porcelain(), [])
        # a revert can itself be reverted, which brings the change back
        rev = r.sha("HEAD")
        res = r.run("revert", "HEAD")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.read("f.txt"), b"ONE\n2\n3\n4\nFIVE\n")
        self.assertEqual(r.read("n.txt"), b"n\n")
        self.assertTrue(r.message().startswith('Revert "Revert "c2 edits line one and adds n""\n'), r.message())
        self.assertEqual(r.parents(), [rev])

    def test_revert_root_commit_empties_the_tree(self):
        r = self.repo
        root = self.seed()
        res = r.run("revert", "HEAD")
        self.assertEqual(res.returncode, 0, res)
        self.assertFalse(r.exists("f.txt"))
        self.assertFalse(r.exists("other.txt"))
        self.assertEqual(r.ls_files(), [])
        self.assertEqual(r.parents(), [root])
        self.assertEqual(r.tree(), "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "the empty tree")
        self.assertEqual(r.porcelain(), [])

    def test_revert_conflict_markers_label_state_status_continue(self):
        r = self.repo
        r.write("f.txt", b"1\n2\n3\n")
        c1 = r.commit_all("first")
        r.write("f.txt", b"1\nTWO\n3\n")
        c2 = r.commit_all("second")
        r.write("f.txt", b"1\nTWO-again\n3\n")
        c3 = r.commit_all("third")

        res = r.run("revert", c2, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (content): Merge conflict in f.txt", res.out, res)
        self.assertIn(f"error: could not revert {c2[:7]}... second", res.err, res)
        self.assertIn("revert --continue", res.err, res)
        self.assertEqual(r.read("f.txt"),
                         b"1\n<<<<<<< HEAD\nTWO-again\n=======\n2\n>>>>>>> parent of " + c2[:7].encode()
                         + b" (second)\n3\n")
        self.assertEqual(r.sha("HEAD"), c3)
        self.assertEqual(r.state("REVERT_HEAD"), c2)
        self.assertIsNone(r.state("CHERRY_PICK_HEAD"))
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertEqual(r.state("ORIG_HEAD"), c3)
        msg = r.state("MERGE_MSG") or ""
        self.assertTrue(msg.startswith('Revert "second"'), msg)
        self.assertIn(f"This reverts commit {c2}.", msg)
        self.assertIn("#\tf.txt", msg)
        self.assertEqual(r.stages_of("f.txt"), {1, 2, 3})
        self.assertEqual(r.porcelain(), ["UU f.txt"])
        long = r.run("status").out
        self.assertIn(f"You are currently reverting commit {c2[:7]}.", long)
        self.assertIn('(fix conflicts and run "minigit revert --continue")', long)
        self.assertIn("\tboth modified:   f.txt", long)
        self.assertEqual(r.sha("REVERT_HEAD"), c2)
        self.assertEqual(r.run("cat-file", "-p", "REVERT_HEAD:f.txt").stdout, b"1\nTWO\n3\n")

        # the other commands refuse while the revert is open
        res = r.run("merge", c1, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have not concluded your revert (REVERT_HEAD exists)", res.err, res)
        res = r.run("cherry-pick", c1, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have not concluded your revert (REVERT_HEAD exists)", res.err, res)
        r.run("branch", "elsewhere", c1)
        res = r.run("switch", "elsewhere", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("revert in progress", res.err, res)
        res = r.run("reset", "--soft", "HEAD", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("cannot do a soft reset in the middle of a revert", res.err, res)
        res = r.run("cherry-pick", "--abort", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("there is no cherry-pick to abort (CHERRY_PICK_HEAD missing)", res.err, res)
        self.assertEqual(r.state("REVERT_HEAD"), c2, "the wrong --abort changes nothing")

        r.write("f.txt", b"1\n2\n3\n")
        r.run("add", "f.txt")
        self.assertIn('(all conflicts fixed: run "minigit revert --continue")', r.run("status").out)
        res = r.run("revert", "--continue")
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), SUMMARY_RE + 'Revert "second"$')
        head = r.sha("HEAD")
        self.assertEqual(r.parents(head), [c3])
        self.assertEqual(r.message(head), f'Revert "second"\n\nThis reverts commit {c2}.\n')
        self.assertEqual(r.header(head, "author"), ["QA Tester <qa@example.com> 1700000000 +0000"])
        self.assertIsNone(r.state("REVERT_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.porcelain(), [])
        self.assertNotIn("reverting", r.run("status").out)
        res = r.run("revert", "--continue", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("no revert in progress (REVERT_HEAD missing)", res.err, res)

    def test_revert_abort_and_reset_hard_restore_head_state(self):
        r = self.repo
        r.write("f.txt", b"1\n2\n3\n")
        r.write("other.txt", b"o\n")
        r.commit_all("first")
        r.write("f.txt", b"1\nTWO\n3\n")
        c2 = r.commit_all("second")
        r.write("f.txt", b"1\nTWO-again\n3\n")
        c3 = r.commit_all("third")
        r.write("other.txt", b"dirty\n")

        res = r.run("revert", c2, check=False)
        self.assertEqual(res.returncode, 1, res)
        res = r.run("revert", "--abort")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(res.out, "", res)
        self.assertEqual(r.read("f.txt"), b"1\nTWO-again\n3\n")
        self.assertEqual(r.read("other.txt"), b"dirty\n", "unrelated local change survives --abort")
        self.assertEqual(r.stages_of("f.txt"), {0})
        self.assertEqual(r.porcelain(), [" M other.txt"])
        self.assertEqual(r.sha("HEAD"), c3)
        self.assertIsNone(r.state("REVERT_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        res = r.run("revert", "--abort", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("there is no revert to abort (REVERT_HEAD missing)", res.err, res)

        res = r.run("revert", c2, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(r.state("REVERT_HEAD"), c2)
        res = r.run("reset", "--hard")
        self.assertEqual(res.returncode, 0, res)
        self.assertIsNone(r.state("REVERT_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.read("f.txt"), b"1\nTWO-again\n3\n")
        self.assertEqual(r.porcelain(), [])

    def test_revert_no_commit_stages_the_inverse_and_commit_uses_prepared_message(self):
        r = self.repo
        c1, c2, c3 = self.three_commits()
        res = r.run("revert", "-n", c2)
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(res.out, "", res)
        self.assertEqual(r.sha("HEAD"), c3)
        self.assertEqual(sorted(r.porcelain()), ["D  n.txt", "M  f.txt"])
        self.assertEqual(r.read("f.txt"), b"1\n2\n3\n4\nFIVE\n")
        self.assertFalse(r.exists("n.txt"))
        self.assertIsNone(r.state("REVERT_HEAD"))
        self.assertNotIn("reverting", r.run("status").out)
        self.assertEqual(r.state("MERGE_MSG"), f'Revert "c2 edits line one and adds n"\n\nThis reverts commit {c2}.')
        res = r.run("commit")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.message(), f'Revert "c2 edits line one and adds n"\n\nThis reverts commit {c2}.\n')
        self.assertEqual(r.parents(), [c3])
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.porcelain(), [])

    def test_revert_merge_commit_needs_mainline(self):
        r = self.repo
        base = self.seed()
        feat = self.feat_commit({"f-side.txt": b"f\n"}, msg="feat adds f-side")
        r.write("m-side.txt", b"m\n")
        mn = r.commit_all("main adds m-side")
        r.run("merge", "feat")
        merge = r.sha("HEAD")
        self.assertEqual(r.parents(merge), [mn, feat])

        res = r.run("revert", "HEAD", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"commit {merge[:7]} is a merge but no -m option was given", res.err, res)
        self.assertEqual(r.sha("HEAD"), merge)
        res = r.run("revert", "-m", "0", "HEAD", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"commit {merge[:7]} does not have parent 0", res.err, res)
        res = r.run("revert", "-m", "1", mn, check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"mainline was specified but commit {mn[:7]} is not a merge", res.err, res)

        res = r.run("revert", "-m", "1", "HEAD")
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), SUMMARY_RE + "Revert \"Merge branch 'feat'\"$")
        self.assertEqual(r.ls_files(), ["f.txt", "m-side.txt", "other.txt"], "back to the first parent's tree")
        self.assertEqual(r.tree(), r.tree(mn))
        self.assertEqual(r.parents(), [merge])
        self.assertEqual(r.message(), f"Revert \"Merge branch 'feat'\"\n\nThis reverts commit {merge}.\n")

    def test_revert_refuses_during_open_merge_and_with_dirty_affected_file(self):
        r = self.repo
        self.seed()
        self.feat_commit({"f.txt": b"a\nb\nC-feat\n"})
        r.write("f.txt", b"a\nb\nC-main\n")
        main = r.commit_all("main work")
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        res = r.run("revert", "HEAD", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have not concluded your merge (MERGE_HEAD exists)", res.err, res)
        res = r.run("cherry-pick", "HEAD~1", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have not concluded your merge (MERGE_HEAD exists)", res.err, res)
        r.run("merge", "--abort")

        r.write("f.txt", b"dirty\n")
        res = r.run("revert", "HEAD", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("would be overwritten by revert", res.err, res)
        self.assertIn("\tf.txt", res.err, res)
        self.assertEqual(r.read("f.txt"), b"dirty\n")
        self.assertEqual(r.sha("HEAD"), main)
        self.assertIsNone(r.state("REVERT_HEAD"))

    def test_revert_on_detached_head_and_by_short_sha(self):
        r = self.repo
        c1, c2, c3 = self.three_commits()
        r.run("checkout", c3)
        res = r.run("revert", c2[:7])
        self.assertEqual(res.returncode, 0, res)
        self.assertRegex(res.out.strip(), r'^\[detached HEAD [0-9a-f]{7}\] Revert "c2 edits line one and adds n"$')
        self.assertEqual(r.parents(), [c3])
        self.assertEqual(r.sha("main"), c3, "the branch left behind does not move")
        self.assertEqual(r.read("f.txt"), b"1\n2\n3\n4\nFIVE\n")
        self.assertFalse(r.exists("n.txt"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Black-box QA suite for the "merge" area of minigit.

Drives the real CLI (``python -m minigit``) as a subprocess inside temporary
repositories. Nothing here imports the ``minigit`` package or ``tests/``.
Expectations come from SPEC.md section 6 ("merge", "merge --abort",
"merge_trees", "merge3") and from how real git behaves.

Run from E:\\ideas\\minigit with::

    python -m unittest tests_blackbox.test_merge
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 60

BASE = b"a\nb\nc\n"
OURS = b"a\nb\nC-ours\n"
THEIRS = b"a\nB-theirs\nc\n"


# ---------------------------------------------------------------------------
# helpers
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


class Repo:
    """One working repository driven through the CLI."""

    def __init__(self, path: str, env: dict) -> None:
        self.path = path
        self.env = env
        os.makedirs(path, exist_ok=True)

    # -- process -----------------------------------------------------------
    def run(self, *args: str, check: bool = True, env: dict | None = None) -> Result:
        argv = [str(a) for a in args]
        proc = subprocess.run([sys.executable, "-m", "minigit", *argv], cwd=self.path,
                              env=env or self.env, capture_output=True, timeout=TIMEOUT)
        res = Result(argv, proc.returncode, proc.stdout, proc.stderr)
        if check and res.returncode != 0:
            raise AssertionError(f"command failed:\n{res}")
        return res

    # -- files -------------------------------------------------------------
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

    # -- queries -----------------------------------------------------------
    def sha(self, rev: str = "HEAD") -> str:
        return self.run("rev-parse", rev).out.strip()

    def commit_text(self, rev: str = "HEAD") -> str:
        return self.run("cat-file", "-p", rev).out

    def parents(self, rev: str = "HEAD") -> list[str]:
        return [ln.split(" ", 1)[1] for ln in self.commit_text(rev).splitlines()
                if ln.startswith("parent ")]

    def message(self, rev: str = "HEAD") -> str:
        text = self.commit_text(rev)
        return text.split("\n\n", 1)[1]

    def porcelain(self) -> list[str]:
        return [ln for ln in self.run("status", "--porcelain").out.splitlines() if ln]

    def ls_stages(self) -> list[tuple[str, int, str]]:
        """(path, stage, sha) from ``ls-files -s`` (``<mode> <sha> <stage>\\t<path>``)."""
        rows = []
        for ln in self.run("ls-files", "-s").out.splitlines():
            if not ln.strip():
                continue
            meta, _, path = ln.partition("\t")
            parts = meta.split()
            rows.append((path, int(parts[2]), parts[1]))
        return rows

    def stages_of(self, path: str) -> set[int]:
        return {st for p, st, _ in self.ls_stages() if p == path}

    def commit_all(self, msg: str) -> str:
        self.run("add", ".")
        self.run("commit", "-m", msg)
        return self.sha()


class MergeCase(unittest.TestCase):
    """Common fixture: a temp dir, an isolated environment, a fresh repo."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tmp.name
        self.env = make_env(self.tmp)
        self.repo = Repo(os.path.join(self.tmp, "work"), self.env)
        self.repo.run("init")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- scenario builders --------------------------------------------------
    def seed(self) -> str:
        """Root commit with f.txt = BASE and other.txt."""
        self.repo.write("f.txt", BASE)
        self.repo.write("other.txt", b"other\n")
        return self.repo.commit_all("base")

    def diverge(self, ours: bytes | None = OURS, theirs: bytes | None = THEIRS,
                ours_extra: dict | None = None, theirs_extra: dict | None = None) -> tuple[str, str, str]:
        """base on main, 'feat' edits f.txt (theirs), main edits f.txt (ours).

        ``None`` for a side deletes f.txt on that side. ``*_extra`` are extra
        files written on that side. Returns (base, ours_sha, theirs_sha); HEAD
        is left on main.
        """
        r = self.repo
        base = self.seed()
        r.run("switch", "-c", "feat")
        if theirs is None:
            r.run("rm", "f.txt")
        else:
            r.write("f.txt", theirs)
        for p, d in (theirs_extra or {}).items():
            r.write(p, d)
        theirs_sha = r.commit_all("theirs")
        r.run("switch", "main")
        if ours is None:
            r.run("rm", "f.txt")
        else:
            r.write("f.txt", ours)
        for p, d in (ours_extra or {}).items():
            r.write(p, d)
        ours_sha = r.commit_all("ours")
        return base, ours_sha, theirs_sha

    def conflicted_merge(self) -> tuple[str, str, Result]:
        """Standard content conflict on f.txt. Returns (ours, theirs, merge result)."""
        _, ours, theirs = self.diverge()
        res = self.repo.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (content): Merge conflict in f.txt", res.out, res)
        return ours, theirs, res


EXPECTED_MARKERS = (b"a\n"
                    b"<<<<<<< HEAD\n"
                    b"b\n"
                    b"C-ours\n"
                    b"=======\n"
                    b"B-theirs\n"
                    b"c\n"
                    b">>>>>>> feat\n")


# ---------------------------------------------------------------------------
# fast-forward / up-to-date
# ---------------------------------------------------------------------------

class FastForwardTests(MergeCase):

    def test_ff_moves_head_updates_worktree_and_writes_orig_head(self):
        r = self.repo
        base = self.seed()
        r.run("switch", "-c", "feat")
        r.write("f.txt", THEIRS)
        r.write("new/deep/n.txt", b"n\n")
        feat = r.commit_all("feat work")
        r.run("switch", "main")

        res = r.run("merge", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertIn(f"Updating {base[:7]}..{feat[:7]}", res.out, res)
        self.assertIn("Fast-forward", res.out, res)
        self.assertNotIn("Merge made", res.out, res)
        self.assertEqual(r.sha("HEAD"), feat)
        self.assertEqual(r.sha("main"), feat)
        self.assertEqual(r.read("f.txt"), THEIRS)
        self.assertEqual(r.read("new/deep/n.txt"), b"n\n")
        self.assertEqual(r.state("ORIG_HEAD"), base)
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertEqual(r.porcelain(), [], r.run("status").out)
        # no merge commit was created: linear history of exactly two commits
        self.assertEqual(len(r.run("log", "--oneline").out.splitlines()), 2)
        self.assertEqual(r.parents("HEAD"), [base])

    def test_ff_deletes_files_removed_upstream_and_prunes_empty_dirs(self):
        r = self.repo
        r.write("f.txt", BASE)
        r.write("dir/sub/gone.txt", b"gone\n")
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.run("rm", "dir/sub/gone.txt")
        r.run("commit", "-m", "remove it")
        r.run("switch", "main")
        self.assertTrue(r.exists("dir/sub/gone.txt"))

        r.run("merge", "feat")
        self.assertFalse(r.exists("dir/sub/gone.txt"))
        self.assertFalse(r.exists("dir"), "empty directories left behind after fast-forward")
        self.assertEqual(r.porcelain(), [])

    def test_ff_ignores_dash_m_and_does_not_create_merge_commit(self):
        r = self.repo
        self.seed()
        r.run("switch", "-c", "feat")
        r.write("f.txt", THEIRS)
        feat = r.commit_all("feat")
        r.run("switch", "main")
        res = r.run("merge", "-m", "custom message", "feat")
        self.assertIn("Fast-forward", res.out, res)
        self.assertEqual(r.sha("HEAD"), feat, "git fast-forwards even when -m is given")
        self.assertEqual(len(r.parents("HEAD")), 1)

    def test_no_ff_creates_merge_commit_with_two_parents(self):
        r = self.repo
        base = self.seed()
        r.run("switch", "-c", "feat")
        r.write("f.txt", THEIRS)
        feat = r.commit_all("feat")
        r.run("switch", "main")

        res = r.run("merge", "--no-ff", "feat")
        self.assertIn("Merge made by the 'ort' strategy.", res.out, res)
        self.assertNotIn("Fast-forward", res.out, res)
        head = r.sha("HEAD")
        self.assertNotEqual(head, feat)
        self.assertEqual(r.parents(head), [base, feat], "parents must be [HEAD, theirs] in that order")
        self.assertEqual(r.message(head), "Merge branch 'feat'\n")
        self.assertEqual(r.read("f.txt"), THEIRS)
        self.assertEqual(r.sha("HEAD^2"), feat)
        self.assertEqual(r.state("ORIG_HEAD"), base)
        self.assertEqual(r.porcelain(), [])

    def test_merging_an_ancestor_is_already_up_to_date(self):
        r = self.repo
        self.seed()
        r.write("f.txt", OURS)
        head = r.commit_all("second")
        for rev in ("HEAD~1", "main", "HEAD"):
            res = r.run("merge", rev)
            self.assertEqual(res.out.strip(), "Already up to date.", res)
            self.assertEqual(r.sha("HEAD"), head)
            self.assertIsNone(r.state("MERGE_HEAD"))
        # --no-ff on an ancestor is still "up to date" (git does not create a commit)
        res = r.run("merge", "--no-ff", "HEAD~1")
        self.assertEqual(res.out.strip(), "Already up to date.", res)
        self.assertEqual(r.sha("HEAD"), head)

    def test_ff_refuses_to_overwrite_local_change_and_touches_nothing(self):
        r = self.repo
        base = self.seed()
        r.run("switch", "-c", "feat")
        r.write("f.txt", THEIRS)
        r.commit_all("feat")
        r.run("switch", "main")
        r.write("f.txt", b"dirty local edit\n")

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("would be overwritten by merge", res.err, res)
        self.assertIn("f.txt", res.err, res)
        self.assertEqual(r.read("f.txt"), b"dirty local edit\n")
        self.assertEqual(r.sha("HEAD"), base)
        self.assertIsNone(r.state("MERGE_HEAD"))

    def test_ff_carries_unrelated_dirty_file_and_untracked_file(self):
        r = self.repo
        self.seed()
        r.run("switch", "-c", "feat")
        r.write("f.txt", THEIRS)
        feat = r.commit_all("feat")
        r.run("switch", "main")
        r.write("other.txt", b"dirty other\n")
        r.write("untracked.txt", b"keep me\n")

        r.run("merge", "feat")
        self.assertEqual(r.sha("HEAD"), feat)
        self.assertEqual(r.read("other.txt"), b"dirty other\n")
        self.assertEqual(r.read("untracked.txt"), b"keep me\n")
        self.assertEqual(sorted(r.porcelain()), [" M other.txt", "?? untracked.txt"])

    def test_ff_refuses_when_untracked_file_is_in_the_way(self):
        r = self.repo
        base = self.seed()
        r.run("switch", "-c", "feat")
        r.write("added.txt", b"from feat\n")
        r.commit_all("feat adds")
        r.run("switch", "main")
        r.write("added.txt", b"my own version\n")   # untracked, different content

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("The following untracked working tree files would be overwritten by merge:\n\tadded.txt\n"
                      "Please move or remove them before you merge.", res.err, res)
        self.assertEqual(r.read("added.txt"), b"my own version\n")
        self.assertEqual(r.sha("HEAD"), base)
        # a non-empty untracked directory with that name is refused too, and kept intact
        r.remove("added.txt")
        r.write("added.txt/precious.txt", b"precious\n")
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        # SUPERSEDED: added.txt is a directory here; git says the same "would lose untracked files" line.
        self.assertIn("Updating the following directories would lose untracked files in them:\n\tadded.txt\n",
                      res.err, res)
        self.assertEqual(r.read("added.txt/precious.txt"), b"precious\n")
        self.assertEqual(r.sha("HEAD"), base)
        # an EMPTY untracked directory is not an obstacle: git replaces it with the file
        r.remove("added.txt/precious.txt")
        self.assertTrue(os.path.isdir(r.abs("added.txt")))
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.read("added.txt"), b"from feat\n")
        self.assertEqual(r.porcelain(), [])

    def test_ff_on_detached_head_moves_only_head(self):
        r = self.repo
        base = self.seed()
        r.run("switch", "-c", "feat")
        r.write("f.txt", THEIRS)
        feat = r.commit_all("feat")
        r.run("checkout", base)
        self.assertIn("HEAD detached", r.run("status").out)
        res = r.run("merge", "feat")
        self.assertIn("Fast-forward", res.out, res)
        self.assertEqual(r.sha("HEAD"), feat)
        self.assertEqual(r.sha("main"), base, "the branch left behind must not move")
        self.assertEqual(r.sha("feat"), feat)
        self.assertIn(f"HEAD detached at {feat[:7]}", r.run("status").out)
        self.assertEqual(r.read("f.txt"), THEIRS)
        self.assertEqual(r.porcelain(), [])

    def test_merge_into_unborn_head_checks_out_theirs(self):
        r = self.repo
        base = self.seed()
        # Point HEAD at a branch that does not exist yet and empty the index/worktree.
        r.write_gitfile("HEAD", "ref: refs/heads/orphan\n")
        os.unlink(os.path.join(r.path, ".minigit", "index"))
        r.remove("f.txt")
        r.remove("other.txt")
        self.assertIn("No commits yet", r.run("status").out)

        res = r.run("merge", "main")
        self.assertEqual(res.returncode, 0, res)
        self.assertIn("Fast-forward", res.out, res)
        self.assertEqual(r.sha("HEAD"), base)
        self.assertEqual(r.sha("orphan"), base)
        self.assertEqual(r.read("f.txt"), BASE)
        self.assertEqual(r.read("other.txt"), b"other\n")
        self.assertEqual(r.porcelain(), [])


# ---------------------------------------------------------------------------
# clean three-way merges
# ---------------------------------------------------------------------------

class ThreeWayCleanTests(MergeCase):

    def test_clean_merge_of_edits_to_different_files(self):
        r = self.repo
        base = self.seed()
        r.run("switch", "-c", "feat")
        r.write("theirs.txt", b"theirs\n")
        theirs = r.commit_all("theirs")
        r.run("switch", "main")
        r.write("ours.txt", b"ours\n")
        ours = r.commit_all("ours")

        res = r.run("merge", "feat")
        self.assertEqual(res.out.splitlines()[0], "Merge made by the 'ort' strategy.", res)
        self.assertIn(" theirs.txt | 1 +\n 1 file changed, 1 insertion(+)\n create mode 100644 theirs.txt\n",
                      res.out, res)   # git's diffstat and summary follow
        head = r.sha("HEAD")
        self.assertEqual(r.parents(head), [ours, theirs])
        self.assertEqual(r.message(head), "Merge branch 'feat'\n")
        self.assertEqual(r.read("ours.txt"), b"ours\n")
        self.assertEqual(r.read("theirs.txt"), b"theirs\n")
        self.assertEqual(r.read("f.txt"), BASE)
        self.assertEqual(r.state("ORIG_HEAD"), ours)
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertIn("nothing to commit, working tree clean", r.run("status").out)
        log = r.run("log").out
        self.assertIn(f"Merge: {ours[:7]} {theirs[:7]}", log)
        self.assertEqual(r.sha("HEAD^1"), ours)
        self.assertEqual(r.sha("HEAD^2"), theirs)
        self.assertEqual(r.sha("HEAD~1"), ours)

    def test_clean_merge_of_non_adjacent_edits_to_same_file(self):
        r = self.repo
        r.write("f.txt", b"1\n2\n3\n4\n5\n6\n7\n")
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.write("f.txt", b"1\n2\n3\n4\n5\n6\nSEVEN\n")
        r.commit_all("theirs edits the end")
        r.run("switch", "main")
        r.write("f.txt", b"ONE\n2\n3\n4\n5\n6\n7\n")
        r.commit_all("ours edits the start")

        res = r.run("merge", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.read("f.txt"), b"ONE\n2\n3\n4\n5\n6\nSEVEN\n")
        self.assertEqual(len(r.parents()), 2)
        self.assertEqual(r.porcelain(), [])

    def test_clean_merge_one_side_deletes_other_untouched(self):
        r = self.repo
        self.seed()
        r.run("switch", "-c", "feat")
        r.run("rm", "f.txt")
        r.run("commit", "-m", "theirs deletes f")
        r.run("switch", "main")
        r.write("other.txt", b"other changed\n")
        r.commit_all("ours edits other")

        res = r.run("merge", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertFalse(r.exists("f.txt"), "deletion from theirs must be applied")
        self.assertEqual(r.read("other.txt"), b"other changed\n")
        self.assertEqual(r.porcelain(), [])
        self.assertNotIn("f.txt", r.run("ls-files").out)

    def test_both_sides_identical_change_and_identical_add_merge_cleanly(self):
        r = self.repo
        self.seed()
        r.run("switch", "-c", "feat")
        r.write("f.txt", OURS)
        r.write("same.txt", b"identical\n")
        r.write("only-theirs.txt", b"t\n")
        r.commit_all("theirs")
        r.run("switch", "main")
        r.write("f.txt", OURS)
        r.write("same.txt", b"identical\n")
        r.commit_all("ours")

        res = r.run("merge", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertNotIn("CONFLICT", res.out, res)
        self.assertEqual(r.read("f.txt"), OURS)
        self.assertEqual(r.read("same.txt"), b"identical\n")
        self.assertEqual(r.read("only-theirs.txt"), b"t\n")
        self.assertEqual(len(r.parents()), 2)

    def test_custom_message_is_normalized_and_sha_merge_uses_merge_commit_text(self):
        r = self.repo
        _, ours, theirs = self.diverge(ours=OURS, theirs=BASE, theirs_extra={"t.txt": b"t\n"})
        res = r.run("merge", "-m", "  Custom subject  \n\nbody line   \n\n\n", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.message("HEAD"), "  Custom subject  \n\nbody line\n",
                         "message must be rstrip()-ed and end with exactly one newline")

        # Now a merge by raw sha: default message "Merge commit '<rev>'".
        r.run("switch", "-c", "side", ours)
        r.write("side.txt", b"side\n")
        side = r.commit_all("side")
        r.run("switch", "main")
        res = r.run("merge", side)
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.message("HEAD"), f"Merge commit '{side}'\n")

    def test_merge_remote_tracking_ref_uses_remote_tracking_message(self):
        r = self.repo
        _, ours, theirs = self.diverge(ours=OURS, theirs=BASE, theirs_extra={"t.txt": b"t\n"})
        os.makedirs(os.path.join(r.path, ".minigit", "refs", "remotes", "origin"), exist_ok=True)
        r.write_gitfile("refs/remotes/origin/feat", theirs + "\n")
        res = r.run("merge", "origin/feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.message("HEAD"), "Merge remote-tracking branch 'origin/feat'\n")
        self.assertEqual(r.parents(), [ours, theirs])

    def test_merge_on_detached_head_creates_merge_commit_without_moving_branches(self):
        r = self.repo
        _, ours, theirs = self.diverge(ours=OURS, theirs=BASE, theirs_extra={"t.txt": b"t\n"})
        r.run("checkout", ours)
        self.assertIn("HEAD detached", r.run("status").out)
        res = r.run("merge", "feat")
        self.assertEqual(res.returncode, 0, res)
        head = r.sha("HEAD")
        self.assertEqual(r.parents(head), [ours, theirs])
        self.assertEqual(r.sha("main"), ours, "detached merge must not move the main branch")
        self.assertEqual(r.sha("feat"), theirs)
        self.assertEqual(r.read("t.txt"), b"t\n")

    def test_clean_merge_does_not_commit_unrelated_dirty_content_and_orig_head_undoes_it(self):
        r = self.repo
        _, ours, theirs = self.diverge(ours=OURS, theirs=BASE, theirs_extra={"t.txt": b"t\n"})
        r.write("other.txt", b"dirty, not staged\n")
        res = r.run("merge", "feat")
        self.assertEqual(res.returncode, 0, res)
        merged = r.sha("HEAD")
        self.assertEqual(r.parents(merged), [ours, theirs])
        self.assertEqual(r.run("cat-file", "-p", "HEAD:other.txt").stdout, b"other\n",
                         "the merge commit must be built from the index, not the dirty worktree")
        self.assertEqual(r.read("other.txt"), b"dirty, not staged\n")
        self.assertEqual(r.porcelain(), [" M other.txt"])
        # undo the merge with ORIG_HEAD, the way git users do
        self.assertEqual(r.state("ORIG_HEAD"), ours)
        res = r.run("reset", "--hard", "ORIG_HEAD")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.sha("HEAD"), ours)
        self.assertFalse(r.exists("t.txt"))
        self.assertEqual(r.read("other.txt"), b"other\n")
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.sha("feat"), theirs)

    def test_file_to_directory_conversion_from_theirs_merges_cleanly(self):
        r = self.repo
        r.write("a.txt", b"a\n")
        r.write("conv", b"i am a file\n")
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.run("rm", "conv")
        r.write("conv/inner.txt", b"inside\n")
        r.write("dir/new.txt", b"new\n")
        theirs = r.commit_all("theirs: conv becomes a directory")
        r.run("switch", "main")
        self.assertEqual(r.read("conv"), b"i am a file\n")
        r.write("a.txt", b"a2\n")
        ours = r.commit_all("ours")

        res = r.run("merge", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.parents(), [ours, theirs])
        self.assertTrue(os.path.isdir(r.abs("conv")))
        self.assertEqual(r.read("conv/inner.txt"), b"inside\n")
        self.assertEqual(r.read("dir/new.txt"), b"new\n")
        self.assertEqual(r.read("a.txt"), b"a2\n")
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(sorted(r.run("ls-files").out.split()), ["a.txt", "conv/inner.txt", "dir/new.txt"])

    def test_criss_cross_history_merges_cleanly_when_both_bases_agree(self):
        r = self.repo
        r.write("a.txt", b"a\n")
        r.write("b.txt", b"b\n")
        r.write("x.txt", b"x\n")
        base = r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.write("b.txt", b"b-feat\n")
        f1 = r.commit_all("F1")
        r.run("switch", "main")
        r.write("a.txt", b"a-main\n")
        a1 = r.commit_all("A1")
        r.run("merge", "feat")                       # M1 = A1 + F1
        m1 = r.sha()
        r.run("switch", "feat")
        r.write("x.txt", b"x-feat\n")
        r.commit_all("F2")
        r.run("merge", a1)                           # M2 = F2 + A1: criss-cross with M1
        m2 = r.sha()
        self.assertEqual(len(r.parents(m2)), 2)
        r.run("switch", "main")
        r.write("c.txt", b"c\n")
        a3 = r.commit_all("A3")
        # merge bases of main (A3) and feat (M2) are A1 and F1; both give the same result here
        res = r.run("merge", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertNotIn("CONFLICT", res.out, res)
        self.assertEqual(r.parents(), [a3, m2])
        self.assertEqual(r.read("a.txt"), b"a-main\n")
        self.assertEqual(r.read("b.txt"), b"b-feat\n")
        self.assertEqual(r.read("x.txt"), b"x-feat\n")
        self.assertEqual(r.read("c.txt"), b"c\n")
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.run("fsck").out.strip(), "")
        self.assertIn(base[:7], r.run("log", "--oneline").out)
        self.assertIn(f1[:7], r.run("log", "--oneline").out)

    def test_clean_merge_result_passes_fsck_and_show_diffs_against_the_first_parent(self):
        r = self.repo
        _, ours, theirs = self.diverge(ours=OURS, theirs=BASE, theirs_extra={"t.txt": b"t\n"})
        r.run("merge", "feat")
        self.assertEqual(r.run("fsck").out.strip(), "")
        show = r.run("show", "HEAD").out
        self.assertIn(f"Merge: {ours[:7]} {theirs[:7]}", show)
        # superseded: a merge is shown against its first parent after a line saying so (SPEC "show")
        self.assertIn(f"(merge commit: showing the diff against the first parent {ours[:7]})", show)
        self.assertIn("diff --git a/t.txt b/t.txt", show)


# ---------------------------------------------------------------------------
# conflicts
# ---------------------------------------------------------------------------

class ConflictTests(MergeCase):

    def test_content_conflict_markers_messages_and_exit_code(self):
        r = self.repo
        ours, theirs, res = self.conflicted_merge()
        lines = res.out.splitlines()
        self.assertEqual(lines[:2], ["Auto-merging f.txt", "CONFLICT (content): Merge conflict in f.txt"], res)
        self.assertEqual(lines[-1], "Automatic merge failed; fix conflicts and then commit the result.", res)
        self.assertEqual(res.err, "", "conflicts are reported on stdout, not stderr")
        self.assertEqual(r.read("f.txt"), EXPECTED_MARKERS)
        self.assertEqual(r.read("other.txt"), b"other\n")
        # HEAD did not move; merge state recorded
        self.assertEqual(r.sha("HEAD"), ours)
        self.assertEqual(r.state("MERGE_HEAD"), theirs)
        self.assertEqual(r.state("ORIG_HEAD"), ours)
        self.assertEqual(r.sha("MERGE_HEAD"), theirs)
        msg = r.state("MERGE_MSG") or ""
        self.assertTrue(msg.startswith("Merge branch 'feat'"), msg)
        self.assertIn("# Conflicts:", msg)
        self.assertIn("#\tf.txt", msg)
        # MERGE_HEAD is a usable revision while the merge is open
        self.assertEqual(r.run("log", "--oneline", "-n", "1", "MERGE_HEAD").out.split()[0], theirs[:7])
        self.assertEqual(r.run("cat-file", "-p", "MERGE_HEAD:f.txt").stdout, THEIRS)

    def test_conflict_status_and_index_stages(self):
        r = self.repo
        self.conflicted_merge()
        self.assertEqual(r.porcelain(), ["UU f.txt"])
        long = r.run("status").out
        self.assertIn("On branch main", long)
        self.assertIn("You have unmerged paths.", long)
        self.assertIn("Unmerged paths:", long)
        self.assertIn("\tboth modified:   f.txt", long)
        self.assertNotIn("nothing to commit", long)
        self.assertEqual(r.stages_of("f.txt"), {1, 2, 3})
        self.assertEqual(r.stages_of("other.txt"), {0})
        self.assertIn("* Unmerged path f.txt", r.run("diff").out)

class ResolveTests(MergeCase):
    """Finishing (or failing to finish) a conflicted merge."""

    def test_commit_merge_and_switch_are_refused_during_conflict(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        res = r.run("commit", "-m", "try", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("cannot commit: unresolved conflicts in: f.txt", res.err, res)
        self.assertEqual(r.sha("HEAD"), ours)

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have not concluded your merge (MERGE_HEAD exists)", res.err, res)

        r.run("branch", "elsewhere", ours)
        res = r.run("switch", "elsewhere", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("merge in progress", res.err, res)
        self.assertEqual(r.state("MERGE_HEAD"), theirs, "a refused command must keep the merge state")
        self.assertEqual(r.read("f.txt"), EXPECTED_MARKERS)

    def test_resolve_with_add_and_commit_dash_m(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        r.write("f.txt", b"a\nB-theirs\nC-ours\n")
        r.run("add", "f.txt")
        self.assertEqual(r.stages_of("f.txt"), {0}, "add must clear stages 1-3")
        self.assertEqual(r.porcelain(), ["M  f.txt"])
        long = r.run("status").out
        self.assertIn("All conflicts fixed but you are still merging.", long)
        self.assertIn("\tmodified:   f.txt", long)
        self.assertNotIn("Unmerged paths", long)

        res = r.run("commit", "-m", "resolved")
        self.assertRegex(res.out.strip(), r"^\[main [0-9a-f]{7}\] resolved$")
        head = r.sha("HEAD")
        self.assertEqual(r.parents(head), [ours, theirs])
        self.assertEqual(r.message(head), "resolved\n")
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.run("fsck").out.strip(), "")
        self.assertIn(f"Merge: {ours[:7]} {theirs[:7]}", r.run("log", "-n", "1").out)
        # the branch is now fully merged
        self.assertIn("Deleted branch feat", r.run("branch", "-d", "feat").out)

    def test_resolve_commit_without_dash_m_uses_merge_msg_minus_comments(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        r.write("f.txt", THEIRS)
        r.run("add", "f.txt")
        res = r.run("commit")
        self.assertEqual(res.returncode, 0, res)
        self.assertIn("Merge branch 'feat'", res.out, res)
        self.assertEqual(r.message("HEAD"), "Merge branch 'feat'\n",
                         "'# Conflicts:' comment lines must be dropped from the message")
        self.assertEqual(r.parents(), [ours, theirs])

    def test_resolve_by_taking_theirs_with_checkout_rev_dash_dash_path(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        res = r.run("checkout", "feat", "--", "f.txt", check=False)
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.read("f.txt"), THEIRS)
        self.assertEqual(r.stages_of("f.txt"), {0})
        self.assertEqual(r.porcelain(), ["M  f.txt"])
        r.run("commit", "-m", "took theirs")
        self.assertEqual(r.parents(), [ours, theirs])

    def test_commit_dash_a_does_not_silently_resolve_unmerged_paths(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        r.write("f.txt", b"resolved\n")
        res = r.run("commit", "-am", "resolved via -a", check=False)
        # like git: `commit -a` only re-stages stage-0 entries, an unmerged path is refused
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("cannot commit: unresolved conflicts in: f.txt", res.err, res)
        self.assertEqual(r.sha("HEAD"), ours)
        self.assertEqual(r.state("MERGE_HEAD"), theirs)
        self.assertEqual(r.stages_of("f.txt"), {1, 2, 3})
        self.assertEqual(r.read("f.txt"), b"resolved\n", "the user's edit must be left alone")

    def test_resolve_with_restore_staged_takes_head_version(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        r.run("restore", "--staged", "f.txt")
        self.assertEqual(r.stages_of("f.txt"), {0})
        self.assertEqual(r.read("f.txt"), EXPECTED_MARKERS, "--staged must not touch the worktree")
        self.assertEqual(r.porcelain(), [" M f.txt"])
        self.assertIn("All conflicts fixed but you are still merging.", r.run("status").out)
        r.run("restore", "f.txt")
        self.assertEqual(r.read("f.txt"), OURS)
        self.assertEqual(r.porcelain(), [])
        res = r.run("commit")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.parents(), [ours, theirs])
        self.assertEqual(r.run("cat-file", "-p", "HEAD:f.txt").stdout, OURS)

    def test_merge_commit_allowed_when_resolved_tree_equals_head(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        r.run("checkout", "HEAD", "--", "f.txt")      # resolve by taking ours entirely
        self.assertEqual(r.read("f.txt"), OURS)
        self.assertEqual(r.porcelain(), [], "index now equals HEAD")
        self.assertIn("All conflicts fixed but you are still merging.", r.run("status").out)
        res = r.run("commit", "-m", "kept ours")
        self.assertEqual(res.returncode, 0, res)
        self.assertNotIn("nothing to commit", res.text, res)
        self.assertEqual(r.parents(), [ours, theirs], "a merge commit is created even with an unchanged tree")
        self.assertEqual(r.commit_text("HEAD").split("\n")[0], r.commit_text(ours).split("\n")[0],
                         "tree of the merge commit equals HEAD's tree")
        self.assertIsNone(r.state("MERGE_HEAD"))

    def test_resolve_modify_delete_as_deletion_with_rm_f_and_add_dot(self):
        r = self.repo
        r.write("f.txt", BASE)
        r.write("g.txt", BASE)
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.run("rm", "f.txt")
        r.run("rm", "g.txt")
        r.run("commit", "-m", "theirs deletes both")
        theirs = r.sha()
        r.run("switch", "main")
        r.write("f.txt", OURS)
        r.write("g.txt", OURS)
        ours = r.commit_all("ours edits both")

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(r.porcelain(), ["UD f.txt", "UD g.txt"])
        # f.txt: resolve with rm -f
        res = r.run("rm", "-f", "f.txt")
        self.assertEqual(res.out.strip(), "rm 'f.txt'", res)
        self.assertFalse(r.exists("f.txt"))
        self.assertEqual(r.stages_of("f.txt"), set())
        self.assertEqual(r.porcelain(), ["D  f.txt", "UD g.txt"])
        # g.txt: delete on disk, then `add .` must drop its conflict stages
        r.remove("g.txt")
        r.run("add", ".")
        self.assertEqual(r.stages_of("g.txt"), set())
        self.assertEqual(r.porcelain(), ["D  f.txt", "D  g.txt"])
        r.run("commit", "-m", "both deleted")
        self.assertEqual(r.parents(), [ours, theirs])
        self.assertEqual(r.run("ls-files").out.strip(), "")
        self.assertEqual(r.porcelain(), [])

class ConflictKindsTests(MergeCase):
    """add/add, modify/delete, binary, several files, newline and encoding corner cases."""

    def test_add_add_conflict_has_no_base_stage(self):
        r = self.repo
        base = self.seed()
        r.run("switch", "-c", "feat")
        r.write("g.txt", b"theirs line\n")
        theirs = r.commit_all("theirs adds g")
        r.run("switch", "main")
        r.write("g.txt", b"ours line\n")
        ours = r.commit_all("ours adds g")

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (add/add): Merge conflict in g.txt", res.out, res)
        self.assertEqual(r.read("g.txt"),
                         b"<<<<<<< HEAD\nours line\n=======\ntheirs line\n>>>>>>> feat\n")
        self.assertEqual(r.stages_of("g.txt"), {2, 3})
        self.assertEqual(r.porcelain(), ["AA g.txt"])
        self.assertIn("\tboth added:      g.txt", r.run("status").out)
        self.assertEqual(r.state("MERGE_HEAD"), theirs)
        self.assertEqual(r.sha("HEAD"), ours)

    def test_modify_delete_conflict_keeps_our_content(self):
        r = self.repo
        _, ours, theirs = self.diverge(ours=OURS, theirs=None)
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (modify/delete): f.txt deleted in feat and modified in HEAD.", res.out, res)
        self.assertEqual(r.read("f.txt"), OURS, "worktree keeps the surviving (modified) side")
        self.assertEqual(r.stages_of("f.txt"), {1, 2})
        self.assertEqual(r.porcelain(), ["UD f.txt"])
        self.assertIn("\tdeleted by them: f.txt", r.run("status").out)
        # resolve by keeping ours
        r.run("add", "f.txt")
        r.run("commit", "-m", "keep ours")
        self.assertEqual(r.parents(), [ours, theirs])
        self.assertEqual(r.read("f.txt"), OURS)

    def test_delete_modify_conflict_restores_their_content_and_resolves_with_rm(self):
        r = self.repo
        _, ours, theirs = self.diverge(ours=None, theirs=THEIRS)
        self.assertFalse(r.exists("f.txt"))
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (modify/delete): f.txt deleted in HEAD and modified in feat.", res.out, res)
        self.assertEqual(r.read("f.txt"), THEIRS, "worktree gets the surviving (modified) side")
        self.assertEqual(r.stages_of("f.txt"), {1, 3})
        self.assertEqual(r.porcelain(), ["DU f.txt"])
        self.assertIn("\tdeleted by us:   f.txt", r.run("status").out)
        # resolve by deleting: remove the file and `add` the missing tracked path
        r.remove("f.txt")
        r.run("add", "f.txt")
        self.assertEqual(r.stages_of("f.txt"), set())
        r.run("commit", "-m", "keep deletion")
        self.assertEqual(r.parents(), [ours, theirs])
        self.assertNotIn("f.txt", r.run("ls-files").out)
        self.assertEqual(r.porcelain(), [])

    def test_binary_conflict_keeps_ours_byte_for_byte(self):
        r = self.repo
        base_bin = b"\x00\x01base\x00" * 50
        ours_bin = b"\x00\x02ours\x00\xff" * 50
        theirs_bin = b"\x00\x03theirs\x00\xfe" * 50
        r.write("blob.bin", base_bin)
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.write("blob.bin", theirs_bin)
        r.commit_all("theirs")
        r.run("switch", "main")
        r.write("blob.bin", ours_bin)
        r.commit_all("ours")

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (binary): Merge conflict in blob.bin", res.out, res)
        self.assertEqual(r.read("blob.bin"), ours_bin, "no markers may be written into a binary file")
        self.assertEqual(r.stages_of("blob.bin"), {1, 2, 3})
        self.assertEqual(r.porcelain(), ["UU blob.bin"])

    def test_conflict_alongside_clean_changes_stages_clean_results(self):
        r = self.repo
        _, ours, theirs = self.diverge(theirs_extra={"newdir/added.txt": b"added by theirs\n"},
                                       ours_extra={"ours-only.txt": b"ours\n"})
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(r.read("newdir/added.txt"), b"added by theirs\n")
        self.assertEqual(r.read("ours-only.txt"), b"ours\n")
        self.assertEqual(sorted(r.porcelain()), ["A  newdir/added.txt", "UU f.txt"])
        self.assertEqual(r.stages_of("newdir/added.txt"), {0})

        r.write("f.txt", b"fixed\n")
        r.run("add", "f.txt")
        r.run("commit", "-m", "merge done")
        self.assertEqual(r.parents(), [ours, theirs])
        tree_listing = r.run("ls-files").out.split()
        self.assertEqual(sorted(tree_listing), ["f.txt", "newdir/added.txt", "other.txt", "ours-only.txt"])
        self.assertEqual(r.porcelain(), [])
        # the merged tree really contains the clean result from theirs
        self.assertEqual(r.run("cat-file", "-p", "HEAD:newdir/added.txt").stdout, b"added by theirs\n")

    def test_multiple_conflicts_are_reported_sorted_and_all_recorded(self):
        r = self.repo
        r.write("z.txt", BASE)
        r.write("a.txt", BASE)
        r.write("m/x.txt", BASE)
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        for p in ("z.txt", "a.txt", "m/x.txt"):
            r.write(p, THEIRS)
        r.commit_all("theirs")
        r.run("switch", "main")
        for p in ("z.txt", "a.txt", "m/x.txt"):
            r.write(p, OURS)
        r.commit_all("ours")

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        conflict_lines = [ln for ln in res.out.splitlines() if ln.startswith("CONFLICT")]
        self.assertEqual(conflict_lines, [
            "CONFLICT (content): Merge conflict in a.txt",
            "CONFLICT (content): Merge conflict in m/x.txt",
            "CONFLICT (content): Merge conflict in z.txt",
        ])
        self.assertEqual(r.porcelain(), ["UU a.txt", "UU m/x.txt", "UU z.txt"])
        msg = r.state("MERGE_MSG") or ""
        self.assertIn("#\ta.txt\n#\tm/x.txt\n#\tz.txt", msg + "\n")
        res = r.run("commit", "-m", "x", check=False)
        self.assertIn("cannot commit: unresolved conflicts in: a.txt, m/x.txt, z.txt", res.err, res)

    def test_conflict_without_trailing_newline_gets_newline_before_marker(self):
        r = self.repo
        r.write("f.txt", b"line\n")
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.write("f.txt", b"theirs-no-eol")
        r.commit_all("theirs")
        r.run("switch", "main")
        r.write("f.txt", b"ours-no-eol")
        r.commit_all("ours")

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(r.read("f.txt"),
                         b"<<<<<<< HEAD\nours-no-eol\n=======\ntheirs-no-eol\n>>>>>>> feat\n")

    def test_crlf_file_gets_crlf_markers(self):
        r = self.repo
        r.write("f.txt", b"a\r\nb\r\nc\r\n")
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.write("f.txt", b"a\r\nTHEIRS\r\nc\r\n")
        r.commit_all("theirs")
        r.run("switch", "main")
        r.write("f.txt", b"a\r\nOURS\r\nc\r\n")
        r.commit_all("ours")

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(r.read("f.txt"),
                         b"a\r\n<<<<<<< HEAD\r\nOURS\r\n=======\r\nTHEIRS\r\n>>>>>>> feat\r\n" b"c\r\n")

    def test_adjacent_edits_conflict_like_git(self):
        # ours changes line 3, theirs changes line 2: no unchanged line between the
        # two hunks, so git (and bzr merge3) report a conflict instead of silently merging.
        r = self.repo
        _, ours, _ = self.diverge(ours=b"a\nb\nC\n", theirs=b"a\nB\nc\n")
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (content)", res.out, res)
        self.assertEqual(r.read("f.txt"), b"a\n<<<<<<< HEAD\nb\nC\n=======\nB\nc\n>>>>>>> feat\n")
        self.assertEqual(r.sha("HEAD"), ours)

    def test_unicode_space_path_conflict_with_slash_branch_and_custom_message(self):
        r = self.repo
        p = "sp ace/ünï cödé.txt"
        r.write(p, BASE)
        r.commit_all("base")
        r.run("switch", "-c", "feature/x-1")
        r.write(p, THEIRS)
        theirs = r.commit_all("theirs")
        r.run("switch", "main")
        r.write(p, OURS)
        ours = r.commit_all("ours")

        res = r.run("merge", "-m", "Custom merge msg", "feature/x-1", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn(f"CONFLICT (content): Merge conflict in {p}", res.out, res)
        self.assertEqual(r.read(p), EXPECTED_MARKERS.replace(b">>>>>>> feat\n", b">>>>>>> feature/x-1\n"))
        self.assertEqual(r.porcelain(), [f"UU {p}"])
        msg = r.state("MERGE_MSG") or ""
        self.assertTrue(msg.startswith("Custom merge msg"), msg)
        self.assertIn(f"#\t{p}", msg)
        r.write(p, b"ok\n")
        r.run("add", p)
        r.run("commit")
        self.assertEqual(r.message("HEAD"), "Custom merge msg\n")
        self.assertEqual(r.parents(), [ours, theirs])

    def test_modify_delete_in_nested_dir_recreates_dir_and_merge_works_from_subdir(self):
        r = self.repo
        r.write("sub/dir/f.txt", BASE)
        r.write("keep.txt", b"k\n")
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.write("sub/dir/f.txt", THEIRS)
        theirs = r.commit_all("theirs edits nested file")
        r.run("switch", "main")
        r.run("rm", "sub/dir/f.txt")
        r.run("commit", "-m", "ours deletes nested file")
        ours = r.sha()
        self.assertFalse(r.exists("sub"), "rm prunes the empty directories")

        # run the merge from an unrelated subdirectory of the worktree
        r.write("elsewhere/deeper/.keep", b"")
        sub = Repo(r.abs("elsewhere/deeper"), self.env)
        res = sub.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (modify/delete): sub/dir/f.txt deleted in HEAD and modified in feat.", res.out, res)
        self.assertEqual(r.read("sub/dir/f.txt"), THEIRS, "the surviving side is written back into a recreated dir")
        self.assertEqual(r.stages_of("sub/dir/f.txt"), {1, 3})
        self.assertEqual(sorted(r.porcelain()), ["?? elsewhere/deeper/.keep", "DU sub/dir/f.txt"])
        res = sub.run("merge", "--abort")
        self.assertEqual(res.returncode, 0, res)
        self.assertFalse(r.exists("sub"), "abort removes the file and prunes the recreated directories")
        self.assertEqual(r.porcelain(), ["?? elsewhere/deeper/.keep"])
        self.assertEqual(r.sha("HEAD"), ours)
        self.assertEqual(r.sha("feat"), theirs)

class RefusalTests(MergeCase):
    """Preconditions and safety checks: the merge must refuse and leave everything untouched."""

    def test_merge_refuses_dirty_file_on_deleted_path_and_missing_file_on_modified_path(self):
        r = self.repo
        r.write("f.txt", BASE)
        r.write("doomed.txt", b"d\n")
        r.write("other.txt", b"o\n")
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.write("f.txt", THEIRS)
        r.run("rm", "doomed.txt")
        r.write("other.txt", b"o2\n")
        theirs = r.commit_all("theirs")
        r.run("switch", "main")
        r.write("unrelated.txt", b"x\n")
        ours = r.commit_all("ours")

        # (1) a dirty file on a path theirs deletes cleanly
        r.write("doomed.txt", b"dirty\n")
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("would be overwritten by merge", res.err, res)
        self.assertIn("\tdoomed.txt", res.err, res)
        self.assertEqual(r.read("doomed.txt"), b"dirty\n")
        self.assertEqual(r.read("f.txt"), BASE, "nothing else may be written")
        self.assertEqual(r.read("other.txt"), b"o\n")
        self.assertIsNone(r.state("MERGE_HEAD"))
        r.write("doomed.txt", b"d\n")
        # (2) a tracked file deleted in the worktree (not staged) on a path theirs modifies
        r.remove("other.txt")
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("would be overwritten by merge", res.err, res)
        self.assertIn("\tother.txt", res.err, res)
        self.assertFalse(r.exists("other.txt"))
        self.assertEqual(r.porcelain(), [" D other.txt"])
        self.assertEqual(r.sha("HEAD"), ours)
        # once clean, the same merge goes through
        r.run("restore", "other.txt")
        res = r.run("merge", "feat")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.parents(), [ours, theirs])
        self.assertFalse(r.exists("doomed.txt"))
        self.assertEqual(r.read("other.txt"), b"o2\n")

    def test_untracked_file_where_merge_needs_a_directory_is_refused(self):
        r = self.repo
        r.write("a.txt", b"a\n")
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.write("dir/new.txt", b"new\n")
        r.commit_all("theirs adds dir/new.txt")
        r.run("switch", "main")
        r.write("a.txt", b"a2\n")
        ours = r.commit_all("ours")
        r.write("dir", b"i am a file\n")             # untracked file named like the new directory

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("would be overwritten by merge", res.err, res)
        self.assertIn("\tdir", res.err, res)
        self.assertEqual(r.read("dir"), b"i am a file\n")
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertEqual(r.sha("HEAD"), ours)
        self.assertEqual(r.porcelain(), ["?? dir"])

    def test_stale_index_lock_fails_cleanly_without_leaving_merge_state(self):
        r = self.repo
        _, ours, theirs = self.diverge()
        r.write_gitfile("index.lock", "")
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 128, res)
        self.assertIn("index.lock exists", res.err, res)
        self.assertIn("If no other minigit process is running, delete it.", res.err, res)
        self.assertEqual(r.read("f.txt"), OURS)
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.sha("HEAD"), ours)
        os.unlink(os.path.join(r.path, ".minigit", "index.lock"))
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (content)", res.out, res)
        self.assertFalse(os.path.exists(os.path.join(r.path, ".minigit", "index.lock")),
                         "the lock must be released after the merge")

    def test_unmerged_paths_without_merge_head_block_merge_commit_and_abort(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        os.unlink(os.path.join(r.path, ".minigit", "MERGE_HEAD"))
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("unmerged paths", res.err, res)
        self.assertIn("f.txt", res.err, res)
        res = r.run("commit", "-m", "x", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("cannot commit: unresolved conflicts in: f.txt", res.err, res)
        res = r.run("merge", "--abort", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("there is no merge to abort (MERGE_HEAD missing)", res.err, res)
        self.assertEqual(r.porcelain(), ["UU f.txt"])
        # reset --hard is the way out, like in git
        r.run("reset", "--hard")
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.read("f.txt"), OURS)
        self.assertEqual(r.sha("HEAD"), ours)

    def test_merge_refuses_dirty_worktree_on_conflicting_path_and_touches_nothing(self):
        r = self.repo
        _, ours, theirs = self.diverge(theirs_extra={"added.txt": b"added\n"})
        r.write("f.txt", b"uncommitted work\n")
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("would be overwritten by merge", res.err, res)
        self.assertIn("\tf.txt", res.err, res)
        self.assertEqual(r.read("f.txt"), b"uncommitted work\n")
        self.assertFalse(r.exists("added.txt"), "nothing may be written when the merge is refused")
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.sha("HEAD"), ours)
        self.assertEqual(r.porcelain(), [" M f.txt"])

    def test_merge_refuses_staged_changes(self):
        r = self.repo
        _, ours, theirs = self.diverge(ours=OURS, theirs=BASE, theirs_extra={"t.txt": b"t\n"})
        r.write("staged.txt", b"staged\n")
        r.run("add", "staged.txt")
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("you have staged changes; commit them first", res.err, res)
        self.assertEqual(r.sha("HEAD"), ours)
        self.assertFalse(r.exists("t.txt"))
        self.assertEqual(r.porcelain(), ["A  staged.txt"])

    def test_merge_refuses_unrelated_histories(self):
        r = self.repo
        self.seed()
        r.write_gitfile("HEAD", "ref: refs/heads/orphan\n")
        os.unlink(os.path.join(r.path, ".minigit", "index"))
        r.remove("f.txt")
        r.remove("other.txt")
        r.write("orphan.txt", b"o\n")
        orphan = r.commit_all("orphan root")
        self.assertEqual(r.parents(orphan), [])

        res = r.run("merge", "main", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("refusing to merge unrelated histories", res.err, res)
        self.assertEqual(r.sha("HEAD"), orphan)
        self.assertFalse(r.exists("f.txt"))
        self.assertIsNone(r.state("MERGE_HEAD"))

    def test_file_directory_clash_is_a_conflict_that_abort_undoes(self):
        # SUPERSEDED: like git, a file/directory clash is a CONFLICT (file moved to x~HEAD), not a refusal.
        r = self.repo
        base = self.seed()
        r.run("switch", "-c", "feat")
        r.write("x/inner.txt", b"dir side\n")
        r.commit_all("theirs adds dir x")
        r.run("switch", "main")
        r.write("x", b"file side\n")
        ours = r.commit_all("ours adds file x")

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("CONFLICT (file/directory): directory in the way of x from HEAD; "
                      "moving it to x~HEAD instead.", res.out, res)
        self.assertEqual(r.read("x~HEAD"), b"file side\n")
        self.assertEqual(r.read("x/inner.txt"), b"dir side\n")
        self.assertEqual(r.stages_of("x~HEAD"), {2})
        self.assertEqual(r.stages_of("x/inner.txt"), {0})
        self.assertEqual(sorted(r.porcelain()), ["A  x/inner.txt", "AU x~HEAD", "D  x"])
        self.assertIsNotNone(r.state("MERGE_HEAD"))
        self.assertEqual(r.sha("HEAD"), ours)

        r.run("merge", "--abort")
        self.assertEqual(r.read("x"), b"file side\n")
        self.assertFalse(r.exists("x~HEAD"))
        self.assertFalse(r.exists("x/inner.txt"))
        self.assertTrue(all(st == 0 for _, st, _ in r.ls_stages()))
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertEqual(r.sha("HEAD"), ours)
        self.assertEqual(r.porcelain(), [])

    def test_merge_without_identity_fails_before_writing(self):
        r = self.repo
        _, ours, theirs = self.diverge(ours=OURS, theirs=BASE, theirs_extra={"t.txt": b"t\n"})
        env = dict(self.env)
        env.pop("MINIGIT_AUTHOR_NAME")
        env.pop("MINIGIT_AUTHOR_EMAIL")
        res = r.run("merge", "feat", check=False, env=env)
        self.assertEqual(res.returncode, 128, res)
        self.assertIn("author identity unknown", res.err, res)
        self.assertEqual(r.sha("HEAD"), ours)
        self.assertFalse(r.exists("t.txt"), "a refused merge must not leave files behind")
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertEqual(r.porcelain(), [])
        # a fast-forward needs no identity
        r.run("reset", "--hard", "HEAD")
        r.run("switch", "-c", "ffsrc", "feat")
        r.run("switch", "main")
        r.run("switch", "-c", "behind", "HEAD~1")
        res = r.run("merge", "feat", env=env)
        self.assertIn("Fast-forward", res.out, res)

    def test_usage_errors_exit_129(self):
        r = self.repo
        self.seed()
        res = r.run("merge", check=False)
        self.assertEqual(res.returncode, 129, res)
        res = r.run("merge", "--abort", "feat", check=False)
        self.assertEqual(res.returncode, 129, res)
        res = r.run("merge", "no-such-branch", check=False)
        self.assertEqual(res.returncode, 128, res)   # git: fatal
        self.assertIn("unknown revision 'no-such-branch'", res.err, res)


# ---------------------------------------------------------------------------
# merge --abort
# ---------------------------------------------------------------------------

class AbortTests(MergeCase):

    def test_abort_restores_head_state_byte_for_byte(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        res = r.run("merge", "--abort")
        self.assertEqual(res.returncode, 0, res)
        self.assertEqual(r.read("f.txt"), OURS)
        self.assertEqual(r.read("other.txt"), b"other\n")
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.sha("HEAD"), ours)
        self.assertEqual(r.sha("feat"), theirs)
        self.assertEqual(r.stages_of("f.txt"), {0})
        self.assertEqual(r.porcelain(), [], r.run("status").out)
        self.assertIn("nothing to commit, working tree clean", r.run("status").out)
        self.assertEqual(r.run("diff").stdout, b"")
        self.assertEqual(r.run("diff", "--staged").stdout, b"")
        # ORIG_HEAD still points at the pre-merge head
        self.assertEqual(r.state("ORIG_HEAD"), ours)

    def test_abort_keeps_unrelated_dirty_and_untracked_files(self):
        r = self.repo
        _, ours, theirs = self.diverge()
        r.write("other.txt", b"dirty but unrelated\n")
        r.write("scratch.txt", b"untracked\n")
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        r.run("merge", "--abort")
        self.assertEqual(r.read("other.txt"), b"dirty but unrelated\n")
        self.assertEqual(r.read("scratch.txt"), b"untracked\n")
        self.assertEqual(r.read("f.txt"), OURS)
        self.assertEqual(sorted(r.porcelain()), [" M other.txt", "?? scratch.txt"])

    def test_abort_removes_files_added_by_merge_and_prunes_dirs(self):
        r = self.repo
        self.diverge(theirs_extra={"newdir/deep/added.txt": b"added by theirs\n"})
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertTrue(r.exists("newdir/deep/added.txt"))
        r.run("merge", "--abort")
        self.assertFalse(r.exists("newdir/deep/added.txt"))
        self.assertFalse(r.exists("newdir"), "directories created by the merge must be pruned")
        self.assertNotIn("added.txt", r.run("ls-files").out)
        self.assertEqual(r.porcelain(), [])

    def test_abort_restores_files_deleted_by_merge(self):
        r = self.repo
        r.write("f.txt", BASE)
        r.write("doomed.txt", b"doomed\n")
        r.commit_all("base")
        r.run("switch", "-c", "feat")
        r.write("f.txt", THEIRS)
        r.run("rm", "doomed.txt")
        r.commit_all("theirs deletes doomed, edits f")
        r.run("switch", "main")
        r.write("f.txt", OURS)
        ours = r.commit_all("ours edits f")

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertFalse(r.exists("doomed.txt"), "clean deletion from theirs is applied")
        self.assertEqual(r.porcelain(), ["D  doomed.txt", "UU f.txt"])
        r.run("merge", "--abort")
        self.assertEqual(r.read("doomed.txt"), b"doomed\n")
        self.assertEqual(r.read("f.txt"), OURS)
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.sha("HEAD"), ours)

    def test_abort_after_partial_resolution_discards_staged_resolution(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        r.write("f.txt", b"half resolved\n")
        r.run("add", "f.txt")
        self.assertEqual(r.porcelain(), ["M  f.txt"])
        r.run("merge", "--abort")
        self.assertEqual(r.read("f.txt"), OURS)
        self.assertEqual(r.porcelain(), [])
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertEqual(r.sha("HEAD"), ours)

    def test_abort_of_modify_delete_and_add_add_conflicts(self):
        r = self.repo
        # ours deleted f.txt, theirs modified it; both added g.txt differently
        base = self.seed()
        r.run("switch", "-c", "feat")
        r.write("f.txt", THEIRS)
        r.write("g.txt", b"theirs g\n")
        r.commit_all("theirs")
        r.run("switch", "main")
        r.run("rm", "f.txt")
        r.write("g.txt", b"ours g\n")
        ours = r.commit_all("ours")

        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertEqual(sorted(r.porcelain()), ["AA g.txt", "DU f.txt"])
        self.assertEqual(r.read("f.txt"), THEIRS)
        r.run("merge", "--abort")
        self.assertFalse(r.exists("f.txt"), "f.txt is not in HEAD, abort must delete it again")
        self.assertEqual(r.read("g.txt"), b"ours g\n")
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.sha("HEAD"), ours)

    def test_abort_then_merge_again_reproduces_the_same_conflict(self):
        r = self.repo
        ours, theirs, first = self.conflicted_merge()
        r.run("merge", "--abort")
        second = r.run("merge", "feat", check=False)
        self.assertEqual(second.returncode, 1, second)
        self.assertEqual(second.out, first.out)
        self.assertEqual(r.read("f.txt"), EXPECTED_MARKERS)
        self.assertEqual(r.state("MERGE_HEAD"), theirs)
        # and resolution still works after the round trip
        r.write("f.txt", b"final\n")
        r.run("add", "f.txt")
        r.run("commit", "-m", "final")
        self.assertEqual(r.parents(), [ours, theirs])

    def test_abort_without_merge_in_progress_fails(self):
        r = self.repo
        ours = self.seed()
        res = r.run("merge", "--abort", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("there is no merge to abort (MERGE_HEAD missing)", res.err, res)
        self.assertEqual(r.sha("HEAD"), ours)
        # after a clean merge there is also nothing to abort
        r.run("switch", "-c", "feat")
        r.write("t.txt", b"t\n")
        r.commit_all("t")
        r.run("switch", "main")
        r.write("o.txt", b"o\n")
        r.commit_all("o")
        r.run("merge", "feat")
        res = r.run("merge", "--abort", check=False)
        self.assertEqual(res.returncode, 1, res)
        self.assertIn("there is no merge to abort", res.err, res)

    def test_reset_hard_during_conflict_also_clears_merge_state(self):
        r = self.repo
        ours, theirs, _ = self.conflicted_merge()
        res = r.run("reset", "--hard", "HEAD")
        self.assertEqual(res.returncode, 0, res)
        self.assertIsNone(r.state("MERGE_HEAD"))
        self.assertIsNone(r.state("MERGE_MSG"))
        self.assertEqual(r.read("f.txt"), OURS)
        self.assertEqual(r.porcelain(), [])
        self.assertEqual(r.sha("HEAD"), ours)
        # a soft reset during a conflict is refused, like git
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        res = r.run("reset", "--soft", "HEAD~1", check=False)
        self.assertEqual(res.returncode, 128, res)   # git: fatal
        self.assertEqual(r.state("MERGE_HEAD"), theirs)
        self.assertEqual(r.sha("HEAD"), ours)

    def test_abort_keeps_untracked_file_inside_merge_created_directory(self):
        r = self.repo
        self.diverge(theirs_extra={"newdir/added.txt": b"added by theirs\n"})
        res = r.run("merge", "feat", check=False)
        self.assertEqual(res.returncode, 1, res)
        r.write("newdir/mine.txt", b"mine\n")      # user drops an untracked file into the new dir
        r.run("merge", "--abort")
        self.assertFalse(r.exists("newdir/added.txt"))
        self.assertEqual(r.read("newdir/mine.txt"), b"mine\n", "abort must not delete untracked files")
        self.assertEqual(r.porcelain(), ["?? newdir/mine.txt"])
        self.assertEqual(r.read("f.txt"), OURS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

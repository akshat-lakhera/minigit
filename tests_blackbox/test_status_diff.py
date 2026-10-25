"""Black-box QA suite for the status / diff area of minigit.

Every test drives the real CLI (``python -m minigit ...``) as a subprocess in a
fresh temporary directory. Expectations come from SPEC.md section 6 ("status",
"diff") and, where the SPEC is silent, from how real git behaves.

Run from E:\\ideas\\minigit with:
    python -m unittest tests_blackbox.test_status_diff

A few fixtures are written straight to disk in the formats the SPEC defines
(loose zlib objects in section 2, the binary index in section 3). That lets the
suite build states the CLI alone cannot make on Windows (executable modes) or
at all (stage-2-only / stage-3-only index entries).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 60
FIXED_DATE = "1700000000 +0000"


# ----------------------------------------------------------------------------- helpers
def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def short(data: bytes) -> str:
    return blob_sha(data)[:7]


def _case_insensitive_fs(where: str) -> bool:
    probe = os.path.join(where, "CaseProbe.tmp")
    with open(probe, "wb") as f:
        f.write(b"x")
    try:
        return os.path.exists(os.path.join(where, "caseprobe.TMP"))
    finally:
        os.unlink(probe)


class Result:
    def __init__(self, args, proc: subprocess.CompletedProcess):
        self.args = args
        self.rc = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", "replace")

    @property
    def errtext(self) -> str:
        return self.err.decode("utf-8", "replace")

    def __str__(self) -> str:
        return (f"minigit {' '.join(self.args)} -> rc={self.rc}\n--- stdout ---\n{self.text}"
                f"\n--- stderr ---\n{self.errtext}")


class MinigitCase(unittest.TestCase):
    """A temp dir, an isolated environment and a repo at ``self.repo``."""

    maxDiff = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mg-qa-sd-", ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("MINIGIT_") and k not in ("PYTHONPATH",)}
        env.update({
            "PYTHONPATH": ROOT,
            "PYTHONIOENCODING": "utf-8",
            "HOME": home,
            "USERPROFILE": home,
            "MINIGIT_GLOBAL_CONFIG": os.path.join(home, ".minigitconfig"),
            "MINIGIT_CREDENTIALS": os.path.join(home, ".minigit-credentials.json"),
            "MINIGIT_AUTHOR_NAME": "QA Tester",
            "MINIGIT_AUTHOR_EMAIL": "qa@example.com",
            "MINIGIT_AUTHOR_DATE": FIXED_DATE,
        })
        self.env = env
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)

    # -- running the CLI
    def mg(self, *args, cwd=None, check=True, env=None, timeout=TIMEOUT) -> Result:
        args = [str(a) for a in args]
        proc = subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd or self.repo,
                              env=env or self.env, capture_output=True, timeout=timeout)
        res = Result(args, proc)
        if check and res.rc != 0:
            self.fail(f"command failed unexpectedly:\n{res}")
        return res

    def init(self) -> None:
        self.mg("init")

    def commit(self, msg="c") -> str:
        self.mg("commit", "-m", msg)
        return self.rev("HEAD")

    def rev(self, spec: str) -> str:
        return self.mg("rev-parse", spec).text.strip()

    def porcelain(self, cwd=None) -> list[str]:
        return self.mg("status", "--porcelain", cwd=cwd).text.splitlines()

    def long_status(self, cwd=None) -> str:
        return self.mg("status", cwd=cwd).text

    def diff(self, *args, cwd=None) -> bytes:
        return self.mg("diff", *args, cwd=cwd).out

    # -- the file system
    def path(self, rel: str) -> str:
        return os.path.join(self.repo, *rel.split("/"))

    def write(self, rel: str, data: bytes | str) -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def remove(self, rel: str) -> None:
        p = self.path(rel)
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)

    def backdate(self, rel: str, seconds: int = 1000) -> None:
        """Move a file's mtime into the past so the index entry is not 'racy'."""
        t = time.time_ns() - seconds * 1_000_000_000
        os.utime(self.path(rel), ns=(t, t))

    # -- on-disk fixtures in SPEC formats (section 2 objects, section 3 index)
    def write_object(self, obj_type: str, data: bytes) -> str:
        raw = obj_type.encode() + b" " + str(len(data)).encode() + b"\0" + data
        sha = hashlib.sha1(raw).hexdigest()
        d = os.path.join(self.repo, ".minigit", "objects", sha[:2])
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, sha[2:])
        if not os.path.exists(p):
            with open(p, "wb") as f:
                f.write(zlib.compress(raw))
        return sha

    def write_flat_tree(self, entries: list[tuple[str, str, str]]) -> str:
        """entries = (mode, name, sha) of files at the root (no subdirectories)."""
        entries = sorted(entries, key=lambda e: e[1].encode())
        data = b"".join(m.encode() + b" " + n.encode() + b"\0" + bytes.fromhex(s) for m, n, s in entries)
        return self.write_object("tree", data)

    def write_commit(self, tree: str, parents: list[str], message: str) -> str:
        lines = [f"tree {tree}"] + [f"parent {p}" for p in parents]
        sig = f"QA Tester <qa@example.com> {FIXED_DATE}"
        lines += [f"author {sig}", f"committer {sig}", "", message]
        return self.write_object("commit", ("\n".join(lines) + "\n").encode())

    def set_branch(self, name: str, sha: str) -> None:
        p = os.path.join(self.repo, ".minigit", "refs", "heads", *name.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", newline="\n") as f:
            f.write(sha + "\n")

    def write_index(self, entries: list[tuple[str, int, str, str]]) -> None:
        """entries = (path, stage, mode, sha); written in the SPEC section 3 format."""
        entries = sorted(entries, key=lambda e: (e[0], e[1]))
        body = struct.pack(">4sII", b"MIDX", 1, len(entries))
        for path, stage, mode, sha in entries:
            pb = path.encode("utf-8")
            body += struct.pack(">I20sQQBH", int(mode, 8), bytes.fromhex(sha), 0, 0, stage, len(pb)) + pb
        body += hashlib.sha1(body).digest()
        with open(os.path.join(self.repo, ".minigit", "index"), "wb") as f:
            f.write(body)

    # -- assertions
    def assertPorcelain(self, expected: list[str], cwd=None) -> None:
        lines = self.porcelain(cwd=cwd)
        self.assertEqual(sorted(lines), sorted(expected), "status --porcelain entries differ")
        paths = [ln[3:] for ln in lines]
        self.assertEqual(paths, sorted(paths), f"porcelain must be sorted by path: {lines}")


# ============================================================================= status
class StatusBasicsTest(MinigitCase):

    def test_unborn_repo_long_format_exact(self):
        self.init()
        self.assertEqual(self.long_status(),
                         "On branch main\n\nNo commits yet\n\nnothing to commit, working tree clean\n")
        self.assertEqual(self.mg("status", "--porcelain").out, b"")
        self.write("f", "x\n")
        self.assertEqual(self.long_status(),
                         "On branch main\n\nNo commits yet\n\nUntracked files:\n\tf\n")
        self.mg("add", "f")
        self.assertEqual(self.long_status(),
                         "On branch main\n\nNo commits yet\n\nChanges to be committed:\n\tnew file:   f\n")

    def test_clean_after_commit_and_flag_aliases(self):
        self.init()
        self.write("a.txt", "a\n")
        self.mg("add", ".")
        self.commit("c1")
        self.assertEqual(self.long_status(), "On branch main\nnothing to commit, working tree clean\n")
        self.write("a.txt", "a2\n")
        outs = {flag: self.mg("status", flag).out for flag in ("-s", "--short", "--porcelain")}
        self.assertEqual(set(outs.values()), {b" M a.txt\n"}, outs)

    def test_every_xy_combination(self):
        """All X (index vs HEAD) x Y (worktree vs index) codes the SPEC lists, checked against git."""
        self.init()
        for name in ("mm", "md", "dd", "keep", "mod", "del", "smod", "back", "clean"):
            self.write(f"{name}.txt", f"{name}\n")
        self.mg("add", ".")
        self.commit("base")
        self.write("mm.txt", "mm2\n"); self.mg("add", "mm.txt"); self.write("mm.txt", "mm3\n")       # MM
        self.write("md.txt", "md2\n"); self.mg("add", "md.txt"); self.remove("md.txt")               # MD
        self.mg("rm", "dd.txt")                                                                      # D
        self.mg("rm", "--cached", "keep.txt")                                                        # D  + ??
        self.write("mod.txt", "mod2\n")                                                              #  M
        self.remove("del.txt")                                                                       #  D
        self.write("smod.txt", "smod2\n"); self.mg("add", "smod.txt")                                # M
        self.write("back.txt", "back2\n"); self.mg("add", "back.txt"); self.write("back.txt", "back\n")  # MM (W == H)
        self.write("new.txt", "n\n"); self.mg("add", "new.txt")                                      # A
        self.write("am.txt", "a\n"); self.mg("add", "am.txt"); self.write("am.txt", "a2\n")          # AM
        self.write("ad.txt", "a\n"); self.mg("add", "ad.txt"); self.remove("ad.txt")                 # AD
        self.write("untracked.txt", "u\n")                                                           # ??
        self.assertPorcelain([
            "AD ad.txt", "AM am.txt", "MM back.txt", "D  dd.txt", " D del.txt",
            "D  keep.txt", "?? keep.txt", "MD md.txt", "MM mm.txt", " M mod.txt",
            "A  new.txt", "M  smod.txt", "?? untracked.txt",
        ])

    def test_long_format_sections_exact(self):
        self.init()
        for n in ("a.txt", "b.txt", "c.txt"):
            self.write(n, n + "\n")
        self.mg("add", ".")
        self.commit("base")
        self.write("a.txt", "a2\n"); self.mg("add", "a.txt")
        self.mg("rm", "c.txt")
        self.write("n.txt", "n\n"); self.mg("add", "n.txt"); self.write("n.txt", "n2\n")
        self.write("b.txt", "b2\n")
        self.write("u.txt", "u\n")
        self.assertEqual(self.long_status(), (
            "On branch main\n"
            "\n"
            "Changes to be committed:\n"
            "\tmodified:   a.txt\n"
            "\tdeleted:    c.txt\n"
            "\tnew file:   n.txt\n"
            "\n"
            "Changes not staged for commit:\n"
            "\tmodified:   b.txt\n"
            "\tmodified:   n.txt\n"
            "\n"
            "Untracked files:\n"
            "\tu.txt\n"))

    def test_untracked_files_listed_individually_and_empty_dirs_hidden(self):
        self.init()
        self.write("deep/er/x.txt", "x\n")
        self.write("deep/y.txt", "y\n")
        os.makedirs(self.path("empty/nested"))
        self.assertEqual(self.porcelain(), ["?? deep/er/x.txt", "?? deep/y.txt"])

    def test_file_replaced_by_directory_and_directory_replaced_by_file(self):
        self.init()
        self.write("x", "file x\n")
        self.write("d/f", "in d\n")
        self.mg("add", ".")
        self.commit("base")
        self.remove("x"); self.write("x/inner", "now a dir\n")
        self.remove("d"); self.write("d", "now a file\n")
        self.assertPorcelain([" D d/f", "?? d", " D x", "?? x/inner"])
        out = self.diff()
        self.assertIn(b"diff --git a/x b/x\ndeleted file mode 100644\n", out)
        self.assertIn(b"diff --git a/d/f b/d/f\ndeleted file mode 100644\n", out)

    def test_porcelain_paths_are_repo_relative_from_subdirectory(self):
        self.init()
        self.write("top.txt", "t\n")
        self.write("sub/inner.txt", "i\n")
        self.mg("add", ".")
        self.commit("base")
        self.write("top.txt", "t2\n")
        self.write("sub/inner.txt", "i2\n")
        self.write("sub/new.txt", "n\n")
        expected = [" M sub/inner.txt", "?? sub/new.txt", " M top.txt"]
        self.assertPorcelain(expected, cwd=self.path("sub"))
        self.assertEqual(self.mg("-C", "sub", "status", "--porcelain").text.splitlines(), expected)

    def test_unicode_and_space_paths_are_utf8(self):
        self.init()
        self.write("café.txt", "c\n")
        self.write("with space.txt", "s\n")
        out = self.mg("status", "--porcelain").out
        self.assertEqual(out, "?? café.txt\n?? with space.txt\n".encode("utf-8"))
        self.mg("add", ".")
        out = self.diff("--cached")
        self.assertIn("diff --git a/café.txt b/café.txt\n".encode("utf-8"), out)
        self.assertIn(b"+++ b/with space.txt", out)

    def test_ignored_untracked_hidden_but_tracked_ignored_file_still_reported(self):
        self.init()
        self.write("keep.log", "tracked\n")
        self.mg("add", "keep.log")
        self.commit("base")
        self.write(".minigitignore", "*.log\r\nbuild/\r\n!important.log\r\n")   # CRLF on purpose
        self.write("build/out.o", "o")
        self.write("a.log", "l")
        self.write("important.log", "i")
        self.write("sub/z.log", "z")
        self.write("sub/z.txt", "z")
        self.write("keep.log", "tracked but changed\n")
        self.assertPorcelain(["?? .minigitignore", "?? important.log", " M keep.log", "?? sub/z.txt"])


class StatusStatCacheTest(MinigitCase):
    """The size+mtime stat cache must never hide a real change (SPEC section 3)."""

    def setUp(self):
        super().setUp()
        self.init()
        self.write("f", "one\n")
        self.write("g", "gee\n")
        self.mg("add", ".")
        self.commit("base")

    def test_touch_without_content_change_is_clean(self):
        self.backdate("f", 50)
        self.assertEqual(self.porcelain(), [])
        self.assertEqual(self.diff(), b"")

    def test_same_size_rewrite_with_preserved_mtime_right_after_add_is_detected(self):
        self.write("f", "two\n")
        self.mg("add", "f")
        st = os.stat(self.path("f"))
        self.write("f", "TWO\n")                       # same size ...
        os.utime(self.path("f"), ns=(st.st_atime_ns, st.st_mtime_ns))   # ... same mtime (racy entry)
        self.assertEqual(self.porcelain(), ["MM f"])
        self.assertIn(b"-two\n+TWO\n", self.diff())

    def test_index_writing_commands_do_not_hide_worktree_changes(self):
        cases = {
            "restore --staged": ["restore", "--staged", "f"],
            "reset -- path": ["reset", "--", "f"],
            "reset HEAD path": ["reset", "HEAD", "f"],
            "reset (mixed)": ["reset"],
        }
        for label, cmd in cases.items():
            with self.subTest(label):
                self.write("f", "two\n")
                self.backdate("f")
                self.mg("add", "f")
                self.mg(*cmd)
                self.assertEqual(self.porcelain(), [" M f"])
                self.assertIn(b"-one\n+two\n", self.diff())
                self.mg("restore", "f")
                self.assertEqual(self.porcelain(), [])

    def test_status_is_read_only_even_while_index_lock_is_held(self):
        self.write("f", "changed\n")
        index = os.path.join(self.repo, ".minigit", "index")
        with open(index, "rb") as fh:
            before = fh.read()
        mtime = os.stat(index).st_mtime_ns
        lock = index + ".lock"
        open(lock, "wb").close()
        self.assertEqual(self.porcelain(), [" M f"])
        self.assertIn(b"+changed\n", self.diff())
        self.assertEqual(self.diff("--cached"), b"")
        self.assertTrue(os.path.exists(lock), "status/diff must not touch another process' index.lock")
        with open(index, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(os.stat(index).st_mtime_ns, mtime)


class StatusErrorsTest(MinigitCase):

    def test_corrupt_index_outside_repo_and_bare_repo(self):
        self.init()
        with open(os.path.join(self.repo, ".minigit", "index"), "wb") as f:
            f.write(b"garbage")
        for cmd in (["status"], ["diff"]):
            r = self.mg(*cmd, check=False)
            self.assertEqual(r.rc, 1, str(r))
            self.assertIn("index file is corrupt; run 'minigit reset' to rebuild it from HEAD", r.errtext)
        outside = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(outside)
        r = self.mg("status", cwd=outside, check=False)
        self.assertEqual(r.rc, 1, str(r))
        self.assertIn("not a minigit repository", r.errtext)
        bare = os.path.join(self.tmp, "bare.git")
        self.mg("init", "--bare", bare, cwd=self.tmp)
        r = self.mg("status", cwd=bare, check=False)
        self.assertEqual(r.rc, 1, str(r))
        self.assertIn("this operation must be run in a work tree", r.errtext)


class StatusWindowsCaseTest(MinigitCase):

    def test_case_only_rename_on_case_insensitive_fs_is_not_untracked(self):
        if not _case_insensitive_fs(self.tmp):
            self.skipTest("needs a case-insensitive file system (Windows / macOS default)")
        self.init()
        self.write("Readme.txt", "readme\n")
        self.mg("add", ".")
        self.commit("base")
        os.rename(self.path("Readme.txt"), self.path("tmp-name"))
        os.rename(self.path("tmp-name"), self.path("README.txt"))
        # git (core.ignorecase=true, the Windows default) reports a clean tree here and
        # `git add -A` keeps the single index entry "Readme.txt".
        self.assertEqual(self.porcelain(), [],
                         "the tracked file is reported as untracked under its new case")
        self.mg("add", ".")
        files = self.mg("ls-files").text.split()
        self.assertEqual(len([f for f in files if f.lower() == "readme.txt"]), 1, files)


# ============================================================================= conflicts
class StatusConflictTest(MinigitCase):

    def setUp(self):
        super().setUp()
        self.init()
        self.write("both.txt", "base\n")
        self.write("ours_del.txt", "base\n")
        self.write("theirs_del.txt", "base\n")
        self.write("clean.txt", "clean\n")
        self.mg("add", ".")
        self.commit("base")
        self.mg("switch", "-c", "feature")
        self.write("both.txt", "feature\n")
        self.write("added.txt", "feature add\n")
        self.write("ours_del.txt", "theirs modified\n")
        self.mg("rm", "theirs_del.txt")
        self.write("clean.txt", "clean\nfeat\n")
        self.mg("add", ".")
        self.commit("feature")
        self.mg("switch", "main")
        self.write("both.txt", "main\n")
        self.write("added.txt", "main add\n")
        self.mg("rm", "ours_del.txt")
        self.write("theirs_del.txt", "ours modified\n")
        self.mg("add", ".")
        self.commit("main")
        r = self.mg("merge", "feature", check=False)
        self.assertEqual(r.rc, 1, str(r))

    def test_unmerged_codes_long_format_and_diff(self):
        self.assertPorcelain(["AA added.txt", "UU both.txt", "M  clean.txt", "DU ours_del.txt",
                              "UD theirs_del.txt"])
        self.assertEqual(self.long_status(), (
            "On branch main\n"
            "You have unmerged paths.\n"
            "\n"
            "Changes to be committed:\n"
            "\tmodified:   clean.txt\n"
            "\n"
            "Unmerged paths:\n"
            "\tboth added:      added.txt\n"
            "\tboth modified:   both.txt\n"
            "\tdeleted by us:   ours_del.txt\n"
            "\tdeleted by them: theirs_del.txt\n"))
        self.assertEqual(self.diff(), (b"* Unmerged path added.txt\n* Unmerged path both.txt\n"
                                       b"* Unmerged path ours_del.txt\n* Unmerged path theirs_del.txt\n"))
        staged = self.diff("--cached")
        self.assertIn(b"* Unmerged path both.txt\n", staged)
        self.assertIn(b"diff --git a/clean.txt b/clean.txt\n", staged)
        self.assertIn(b" clean\n+feat\n", staged)
        self.assertEqual(self.diff("--", "clean.txt"), b"")       # pathspec filters unmerged lines too
        self.assertEqual(self.diff("--", "both.txt"), b"* Unmerged path both.txt\n")

    def test_all_conflicts_fixed_but_still_merging(self):
        self.write("both.txt", "resolved\n")
        self.write("added.txt", "resolved add\n")
        self.mg("add", "both.txt", "added.txt", "theirs_del.txt")
        self.mg("rm", "-f", "ours_del.txt")
        self.assertPorcelain(["M  added.txt", "M  both.txt", "M  clean.txt"])
        self.assertEqual(self.long_status(), (
            "On branch main\n"
            "All conflicts fixed but you are still merging.\n"
            "\n"
            "Changes to be committed:\n"
            "\tmodified:   added.txt\n"
            "\tmodified:   both.txt\n"
            "\tmodified:   clean.txt\n"))
        self.commit("merged")
        self.assertEqual(self.long_status(), "On branch main\nnothing to commit, working tree clean\n")

    def test_rm_resolves_deleted_by_us_conflict_without_force_like_git(self):
        # git: `git rm ours_del.txt` on a DU path succeeds without -f and resolves it.
        r = self.mg("rm", "ours_del.txt", check=False)
        self.assertEqual(r.rc, 0, str(r))
        self.assertNotIn("DU ours_del.txt", self.porcelain())


class StatusCraftedStagesTest(MinigitCase):

    def test_au_and_ua_codes_from_stage_only_entries(self):
        self.init()
        x = self.write_object("blob", b"x\n")
        y = self.write_object("blob", b"y\n")
        self.write_index([
            ("au.txt", 2, "100644", x),
            ("ok.txt", 0, "100644", x),
            ("ua.txt", 3, "100644", y),
            ("uu.txt", 1, "100644", x), ("uu.txt", 2, "100644", x), ("uu.txt", 3, "100644", y),
        ])
        for n in ("au.txt", "ok.txt", "ua.txt", "uu.txt"):
            self.write(n, "x\n")
        self.assertEqual(self.porcelain(), ["AU au.txt", "A  ok.txt", "UA ua.txt", "UU uu.txt"])
        text = self.long_status()
        self.assertIn("Unmerged paths:\n\tadded by us:     au.txt\n\tadded by them:   ua.txt\n"
                      "\tboth modified:   uu.txt\n", text)
        self.assertEqual(self.diff(), b"* Unmerged path au.txt\n* Unmerged path ua.txt\n* Unmerged path uu.txt\n")


# ============================================================================= branch header
class StatusBranchHeaderTest(MinigitCase):

    def setUp(self):
        super().setUp()
        self.init()
        self.shas = []
        for i in (1, 2, 3):
            self.write("f", f"{i}\n")
            self.mg("add", "f")
            self.shas.append(self.commit(f"c{i}"))

    def _set_tracking(self, sha: str | None) -> None:
        p = os.path.join(self.repo, ".minigit", "refs", "remotes", "origin", "main")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if sha is None:
            os.unlink(p)
        else:
            with open(p, "w", newline="\n") as f:
                f.write(sha + "\n")

    def test_detached_head_header(self):
        self.mg("switch", "--detach", self.shas[0])
        self.assertEqual(self.long_status(),
                         f"HEAD detached at {self.shas[0][:7]}\nnothing to commit, working tree clean\n")

    def test_upstream_ahead_behind_diverged_up_to_date(self):
        self.mg("config", "branch.main.remote", "origin")
        self.mg("config", "branch.main.merge", "refs/heads/main")
        c1, c2, c3 = self.shas
        clean = "\nnothing to commit, working tree clean\n"
        self._set_tracking(c3)
        self.assertEqual(self.long_status(),
                         "On branch main\nYour branch is up to date with 'origin/main'.\n" + clean)
        self._set_tracking(c2)
        self.assertIn("Your branch is ahead of 'origin/main' by 1 commit.\n", self.long_status())
        self._set_tracking(c1)
        self.assertIn("Your branch is ahead of 'origin/main' by 2 commits.\n", self.long_status())
        self.mg("reset", "--hard", c1)
        self._set_tracking(c3)
        self.assertEqual(self.long_status(), "On branch main\nYour branch is behind 'origin/main' by 2 "
                                             "commits, and can be fast-forwarded.\n" + clean)
        self._set_tracking(c2)
        self.assertIn("Your branch is behind 'origin/main' by 1 commit, and can be fast-forwarded.\n",
                      self.long_status())
        self.write("f", "side\n")
        self.mg("commit", "-a", "-m", "side")
        self.assertIn("Your branch and 'origin/main' have diverged,\n"
                      "and have 1 and 1 different commits each, respectively.\n", self.long_status())
        self.mg("switch", "--detach", "HEAD")
        self.assertNotIn("origin/main", self.long_status())


class StatusLiveServerTest(MinigitCase):
    """Tracking info produced by a real clone / push / fetch round trip."""

    def _start_server(self, root: str) -> str:
        ready = os.path.join(self.tmp, "ready.txt")
        log = open(os.path.join(self.tmp, "server.log"), "wb")
        self.addCleanup(log.close)
        proc = subprocess.Popen([sys.executable, "-m", "minigit", "serve", "--root", root, "--host",
                                 "127.0.0.1", "--port", "0", "--ready-file", ready, "--quiet"],
                                cwd=self.tmp, env=self.env, stdout=log, stderr=subprocess.STDOUT)

        def stop():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
        self.addCleanup(stop)     # runs before the temp dir cleanup (LIFO)
        deadline = time.time() + 15
        while time.time() < deadline:
            if os.path.exists(ready):
                with open(ready) as f:
                    addr = f.read().strip()
                if addr:
                    return f"http://{addr}"
            if proc.poll() is not None:
                self.fail("server exited early")
            time.sleep(0.1)
        self.fail("server did not become ready")

    def test_clone_push_fetch_tracking_lines_and_remote_diff(self):
        root = os.path.join(self.tmp, "srv")
        self.mg("server", "init", root, cwd=self.tmp)
        token = self.mg("server", "add-user", root, "alice", cwd=self.tmp).text.strip()
        self.mg("server", "create-repo", root, "proj", cwd=self.tmp)
        url = self._start_server(root) + "/proj"
        self.env["MINIGIT_USER"], self.env["MINIGIT_TOKEN"] = "alice", token
        a, b = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        self.mg("clone", url, a, cwd=self.tmp)
        with open(os.path.join(a, "f"), "wb") as f:
            f.write(b"x\n")
        self.mg("add", "f", cwd=a)
        self.mg("commit", "-m", "c1", cwd=a)
        self.mg("push", "-u", "origin", "main", cwd=a)
        uptodate = "On branch main\nYour branch is up to date with 'origin/main'.\n\nnothing to commit, working tree clean\n"
        self.assertEqual(self.long_status(cwd=a), uptodate)
        self.mg("clone", url, b, cwd=self.tmp)
        self.assertEqual(self.long_status(cwd=b), uptodate)
        with open(os.path.join(b, "f"), "ab") as f:
            f.write(b"y\n")
        self.mg("commit", "-a", "-m", "c2", cwd=b)
        self.assertIn("Your branch is ahead of 'origin/main' by 1 commit.\n", self.long_status(cwd=b))
        self.mg("push", cwd=b)
        self.mg("fetch", cwd=a)
        self.assertIn("Your branch is behind 'origin/main' by 1 commit, and can be fast-forwarded.\n",
                      self.long_status(cwd=a))
        out = self.mg("diff", "main", "origin/main", cwd=a).out
        old_sha, new_sha = short(b"x\n"), short(b"x\ny\n")
        self.assertEqual(out, (f"diff --git a/f b/f\nindex {old_sha}..{new_sha} 100644\n"
                               "--- a/f\n+++ b/f\n@@ -1 +1,2 @@\n x\n+y\n").encode())
        self.assertEqual(self.mg("diff", "origin/main", cwd=a).out.count(b"\n-y\n"), 1)


# ============================================================================= diff format
class DiffFormatTest(MinigitCase):

    def setUp(self):
        super().setUp()
        self.init()

    def _diff_after(self, old: bytes, new: bytes, name="f.txt") -> bytes:
        self.write(name, old)
        self.mg("add", name)
        self.mg("commit", "-m", f"base {name}")
        self.write(name, new)
        return self.diff("--", name)

    def _hunks(self, out: bytes) -> bytes:
        i = out.find(b"\n@@ ")
        return out[i + 1:] if i >= 0 else b""

    def test_modified_file_exact_output(self):
        old, new = b"one\ntwo\nthree\n", b"one\n2\nthree\nfour\n"
        self.assertEqual(self._diff_after(old, new), (
            b"diff --git a/f.txt b/f.txt\n"
            + f"index {short(old)}..{short(new)} 100644\n".encode()
            + b"--- a/f.txt\n+++ b/f.txt\n"
            b"@@ -1,3 +1,4 @@\n one\n-two\n+2\n three\n+four\n"))

    def test_no_newline_at_end_of_file_variants(self):
        nl = b"\\ No newline at end of file\n"
        ten = b"\n".join(b"l%d" % i for i in range(1, 11))          # no final newline
        cases = {
            "last line changed, both without EOL": (b"a\nb", b"a\nc",
                                                    b"@@ -1,2 +1,2 @@\n a\n-b\n" + nl + b"+c\n" + nl),
            "EOL added": (b"a", b"a\n", b"@@ -1 +1 @@\n-a\n" + nl + b"+a\n"),
            "EOL removed": (b"a\n", b"a", b"@@ -1 +1 @@\n-a\n+a\n" + nl),
            "line appended after missing EOL": (b"a\nb", b"a\nb\nc",
                                                b"@@ -1,2 +1,3 @@\n a\n-b\n" + nl + b"+b\n+c\n" + nl),
            "change far from the missing EOL": (ten, ten.replace(b"l2\n", b"X\n"),
                                                b"@@ -1,5 +1,5 @@\n l1\n-l2\n+X\n l3\n l4\n l5\n"),
            "context reaches the missing EOL": (ten, ten.replace(b"l9\n", b"X\n"),
                                                b"@@ -6,5 +6,5 @@\n l6\n l7\n l8\n-l9\n+X\n l10\n" + nl),
        }
        for i, (label, (old, new, hunks)) in enumerate(cases.items()):
            with self.subTest(label):
                self.assertEqual(self._hunks(self._diff_after(old, new, f"n{i}.txt")), hunks)

    def test_hunk_ranges_and_grouping(self):
        def lines(n):
            return b"".join(b"l%d\n" % i for i in range(1, n + 1))
        L20, L10, L8 = lines(20), lines(10), lines(8)
        cases = {
            "gap of 6 merges": (L20, L20.replace(b"l5\n", b"X\n").replace(b"l12\n", b"Y\n"),
                                [b"@@ -2,14 +2,14 @@"]),
            "gap of 7 splits": (L20, L20.replace(b"l5\n", b"X\n").replace(b"l13\n", b"Y\n"),
                                [b"@@ -2,7 +2,7 @@", b"@@ -10,7 +10,7 @@"]),
            "far apart": (L20, L20.replace(b"l2\n", b"X\n").replace(b"l18\n", b"Y\n"),
                          [b"@@ -1,5 +1,5 @@", b"@@ -15,6 +15,6 @@"]),
            "pure insert": (L10, L10.replace(b"l5\n", b"l5\nins\n"), [b"@@ -3,6 +3,7 @@"]),
            "pure delete": (L10, L10.replace(b"l5\n", b""), [b"@@ -2,7 +2,6 @@"]),
            "insert at top": (lines(5), b"new\n" + lines(5), [b"@@ -1,3 +1,4 @@"]),
            "delete at end": (L8, lines(6), [b"@@ -4,5 +4,3 @@"]),
            "file emptied": (b"a\nb\n", b"", [b"@@ -1,2 +0,0 @@"]),
            "empty file filled": (b"", b"a\n", [b"@@ -0,0 +1 @@"]),
            "single line": (b"x\n", b"y\n", [b"@@ -1 +1 @@"]),
        }
        for i, (label, (old, new, headers)) in enumerate(cases.items()):
            with self.subTest(label):
                out = self._diff_after(old, new, f"h{i}.txt")
                got = [ln for ln in out.split(b"\n") if ln.startswith(b"@@")]
                self.assertEqual(got, headers, out)

    def test_crlf_and_non_utf8_bytes_are_preserved_verbatim(self):
        out = self._diff_after(b"a\r\nb\r\nc\r\n", b"a\r\nB\r\nc\r\n", "crlf.txt")
        self.assertEqual(self._hunks(out), b"@@ -1,3 +1,3 @@\n a\r\n-b\r\n+B\r\n c\r\n")
        out = self._diff_after(b"caf\xe9\n", b"caf\xe9 au lait\n", "latin1.txt")
        self.assertEqual(self._hunks(out), b"@@ -1 +1 @@\n-caf\xe9\n+caf\xe9 au lait\n")
        out = self._diff_after(b"a\nb\n", b"a\r\nb\n", "eol.txt")
        self.assertEqual(self._hunks(out), b"@@ -1,2 +1,2 @@\n-a\n+a\r\n b\n")

    def test_binary_detection_uses_first_8000_bytes(self):
        out = self._diff_after(b"start\n", b"x" * 7999 + b"\0", "b1.dat")
        self.assertEqual(out, (b"diff --git a/b1.dat b/b1.dat\n"
                               + f"index {short(b'start' + bytes([10]))}..{short(b'x' * 7999 + bytes(1))} 100644\n".encode()
                               + b"Binary files a/b1.dat and b/b1.dat differ\n"))
        out = self._diff_after(b"start\n", b"x" * 8000 + b"\0\n", "b2.dat")
        self.assertNotIn(b"Binary files", out)
        self.assertIn(b"--- a/b2.dat\n+++ b/b2.dat\n@@ -1 +1 @@\n-start\n+", out)
        out = self._diff_after(b"\0old", b"\0new", "b3.dat")
        self.assertTrue(out.endswith(b"Binary files a/b3.dat and b/b3.dat differ\n"), out)
        self.assertNotIn(b"--- a/", out)

    def test_worktree_diff_ignores_untracked_and_shows_deletions(self):
        self.write("keep.txt", "k\n")
        self.write("gone.txt", "g\n")
        self.mg("add", ".")
        self.commit("base")
        self.write("untracked.txt", "u\n")
        self.remove("gone.txt")
        self.assertEqual(self.diff(), (
            b"diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n"
            + f"index {short(b'g' + bytes([10]))}..0000000\n".encode()
            + b"--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-g\n"))
        r = self.mg("diff")
        self.assertEqual(r.rc, 0, "diff exits 0 even when there are differences")

    def test_new_and_deleted_empty_file_have_no_file_headers_like_git(self):
        # git prints only the 3 header lines for an empty file: no ---/+++ (there is no hunk).
        self.write("e", b"")
        self.mg("add", "e")
        self.assertEqual(self.diff("--cached"),
                         b"diff --git a/e b/e\nnew file mode 100644\nindex 0000000..e69de29\n")
        self.commit("empty")
        self.remove("e")
        self.assertEqual(self.diff(), b"diff --git a/e b/e\ndeleted file mode 100644\nindex e69de29..0000000\n")

    def test_many_repeated_lines_diff_is_not_quadratic(self):
        # git needs ~0.2 s here; difflib with autojunk=False is O(n^2) on repeated lines.
        self.write("blank.txt", b"\n" * 8000 + b"end\n")
        self.mg("add", "blank.txt")
        self.commit("blank")
        self.write("blank.txt", b"top\n" + b"\n" * 8000 + b"end2\n")
        start = time.time()
        try:
            r = self.mg("diff", timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("diff of an 8000-line file with repeated lines took more than 10 s (git: ~0.2 s)")
        self.assertIn(b"+top\n", r.out)
        self.assertLess(time.time() - start, 10)


# ============================================================================= diff forms
class DiffFormsTest(MinigitCase):

    def setUp(self):
        super().setUp()
        self.init()

    def test_staged_and_cached_vs_head_including_unborn(self):
        self.write("f", "v1\n")
        self.mg("add", "f")
        unborn = self.diff("--cached")
        self.assertEqual(unborn, (b"diff --git a/f b/f\nnew file mode 100644\n"
                                  + f"index 0000000..{short(b'v1' + bytes([10]))}\n".encode()
                                  + b"--- /dev/null\n+++ b/f\n@@ -0,0 +1 @@\n+v1\n"))
        self.assertEqual(self.diff(), b"")
        self.commit("c1")
        self.write("f", "v2\n")
        self.mg("add", "f")
        self.write("f", "v3\n")
        staged = self.diff("--staged")
        self.assertEqual(staged, self.diff("--cached"))
        self.assertIn(b"-v1\n+v2\n", staged)
        self.assertIn(b"-v2\n+v3\n", self.diff())
        self.assertIn(b"-v1\n+v3\n", self.diff("HEAD"))

    def test_diff_rev_against_worktree(self):
        for n in ("a", "b", "c"):
            self.write(n, n + "\n")
        self.mg("add", ".")
        c1 = self.commit("c1")
        self.write("c", "c2\n")
        self.mg("commit", "-a", "-m", "c2")
        self.write("a", "a-worktree\n")         # unstaged edit
        self.remove("b")                        # unstaged delete
        self.write("n", "n\n")
        self.mg("add", "n")                     # staged new file
        self.write("u", "u\n")                  # untracked: never shown
        out = self.diff("HEAD")
        heads = [ln for ln in out.split(b"\n") if ln.startswith(b"diff --git")]
        self.assertEqual(heads, [b"diff --git a/a b/a", b"diff --git a/b b/b", b"diff --git a/n b/n"])
        self.assertIn(b"diff --git a/b b/b\ndeleted file mode 100644\n", out)
        self.assertIn(b"diff --git a/n b/n\nnew file mode 100644\n", out)
        older = self.diff(c1)
        self.assertIn(b"diff --git a/c b/c\n", older)     # committed after c1, so it differs from c1
        self.assertIn(b"-c\n+c2\n", older)
        self.assertEqual(self.diff("HEAD", "--", "a"), self.diff("--", "a"))

    def test_commit_vs_commit_forms_and_path_order(self):
        for n in ("a-b", "a.txt", "a/x", "a0", "z"):
            self.write(n, n + "\n")
        self.mg("add", ".")
        c1 = self.commit("c1")
        for n in ("a-b", "a.txt", "a/x", "a0"):
            self.write(n, n + " changed\n")
        self.remove("z")
        self.write("new", "new\n")
        self.mg("add", ".")
        c2 = self.commit("c2")
        out = self.diff(c1, c2)
        heads = [ln[len(b"diff --git a/"):].split(b" ")[0] for ln in out.split(b"\n") if ln.startswith(b"diff --git")]
        self.assertEqual(heads, [b"a-b", b"a.txt", b"a/x", b"a0", b"new", b"z"])
        self.assertEqual(self.diff(f"{c1}..{c2}"), out)
        self.assertEqual(self.diff(f"{c1}.."), out)
        self.assertEqual(self.diff("HEAD~1", "HEAD"), out)
        rev = self.diff(f"..{c1}")
        self.assertEqual(rev, self.diff(c2, c1))
        self.assertIn(b"diff --git a/z b/z\nnew file mode 100644\n", rev)
        self.assertIn(b"diff --git a/new b/new\ndeleted file mode 100644\n", rev)
        self.assertEqual(self.diff(c1, c2, "--", "a"), out[out.index(b"diff --git a/a/x"):out.index(b"diff --git a/a0")])
        self.assertEqual(self.diff(c1, c1), b"")

    def test_pathspec_prefix_semantics(self):
        for n in ("src/a", "src2/b", "top", "srcfile"):
            self.write(n, n + "\n")
        self.mg("add", ".")
        self.commit("c1")
        for n in ("src/a", "src2/b", "top", "srcfile"):
            self.write(n, n + " 2\n")

        def files(out: bytes) -> list[bytes]:
            return [ln.split(b" ")[2][2:] for ln in out.split(b"\n") if ln.startswith(b"diff --git")]
        self.assertEqual(files(self.diff("--", "src")), [b"src/a"])
        self.assertEqual(files(self.diff("--", "src/")), [b"src/a"])
        self.assertEqual(files(self.diff("src")), [b"src/a"])
        self.assertEqual(files(self.diff("--", "src", "top")), [b"src/a", b"top"])
        self.assertEqual(files(self.diff("--", ".", cwd=self.path("src2"))), [b"src2/b"])
        self.assertEqual(files(self.diff("--", "b", cwd=self.path("src2"))), [b"src2/b"])
        self.assertEqual(files(self.diff("--", "../top", cwd=self.path("src2"))), [b"top"])
        if os.name == "nt":
            self.assertEqual(files(self.diff("--", "src\\a")), [b"src/a"])
        r = self.mg("diff", "--", "does-not-exist")
        self.assertEqual((r.rc, r.out), (0, b""))
        r = self.mg("diff", "--", "../outside", check=False)
        self.assertEqual(r.rc, 1, str(r))
        self.assertIn("outside repository", r.errtext)

    def test_mode_changes_from_crafted_commits(self):
        hi = self.write_object("blob", b"echo hi\n")
        bye = self.write_object("blob", b"echo bye\n")
        c1 = self.write_commit(self.write_flat_tree([("100644", "run.sh", hi), ("100644", "tool", hi)]), [], "c1")
        c2 = self.write_commit(self.write_flat_tree([("100755", "run.sh", hi), ("100755", "tool", bye),
                                                     ("100755", "newx", hi)]), [c1], "c2")
        self.set_branch("main", c2)
        out = self.diff(c1, c2)
        self.assertEqual(out, (
            b"diff --git a/newx b/newx\nnew file mode 100755\n"
            + f"index 0000000..{hi[:7]}\n".encode()
            + b"--- /dev/null\n+++ b/newx\n@@ -0,0 +1 @@\n+echo hi\n"
            b"diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
            b"diff --git a/tool b/tool\nold mode 100644\nnew mode 100755\n"
            + f"index {hi[:7]}..{bye[:7]}\n".encode()
            + b"--- a/tool\n+++ b/tool\n@@ -1 +1 @@\n-echo hi\n+echo bye\n"))
        # Checking out executables must not look modified (on Windows the mode comes from the index).
        self.mg("reset", "--hard", c2)
        self.assertEqual(self.porcelain(), [])
        self.assertEqual(self.diff(), b"")
        self.write("run.sh", "echo hi\nmore\n")
        self.assertEqual(self.diff(), (b"diff --git a/run.sh b/run.sh\n"
                                       + f"index {hi[:7]}..{short(b'echo hi' + bytes([10]) + b'more' + bytes([10]))} 100755\n".encode()
                                       + b"--- a/run.sh\n+++ b/run.sh\n@@ -1 +1,2 @@\n echo hi\n+more\n"))
        self.mg("add", "run.sh")
        self.assertIn(b" 100755\n", self.diff("--cached"))
        self.assertEqual(self.porcelain(), ["M  run.sh"])

    def test_diff_rev_keeps_head_mode_for_path_missing_from_index(self):
        hi = self.write_object("blob", b"echo hi\n")
        c1 = self.write_commit(self.write_flat_tree([("100755", "run.sh", hi)]), [], "c1")
        self.set_branch("main", c1)
        self.mg("reset", "--hard", c1)
        self.mg("rm", "--cached", "run.sh")
        out = self.diff("HEAD")
        # SPEC "mode from stat": on Windows use the previous index or HEAD mode. Content is unchanged,
        # so no mode change may be invented (git itself would print a deletion of a 100755 file).
        self.assertNotIn(b"new mode 100644", out, out)

    def test_diff_cached_against_a_commit_like_git(self):
        self.write("f", "v1\n")
        self.mg("add", "f")
        c1 = self.commit("c1")
        self.write("f", "v2\n")
        self.mg("commit", "-a", "-m", "c2")
        self.write("f", "v3\n")
        self.mg("add", "f")
        r = self.mg("diff", "--cached", c1, check=False)    # git diff --cached <commit>
        self.assertEqual(r.rc, 0, str(r))
        self.assertIn(b"-v1\n+v3\n", r.out)

    def test_diff_errors_and_exit_codes(self):
        self.write("f", "x\n")
        self.mg("add", "f")
        self.commit("c1")
        r = self.mg("diff", "nosuchrev", check=False)
        self.assertEqual(r.rc, 1, str(r))
        self.assertIn("unknown revision 'nosuchrev'", r.errtext)
        r = self.mg("diff", "HEAD", "HEAD", "HEAD", check=False)
        self.assertEqual(r.rc, 2, str(r))
        r = self.mg("diff", "HEAD~5", check=False)
        self.assertEqual(r.rc, 1, str(r))
        self.assertEqual(self.diff("HEAD", "HEAD"), b"")


if __name__ == "__main__":
    unittest.main()

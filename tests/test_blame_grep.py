"""Acceptance tests for the "blame-grep" feature (design: features/blame-grep.md).

Drives the REAL CLI (``python -m minigit``) through subprocess in temporary
directories. Nothing here imports the product or ``tests.helpers``.

Run from the project root::

    python -m unittest tests.test_blame_grep

One deterministic fixture repository is built once per class (fixed identities
and author dates, so every sha is reproducible). Read-only tests use it
directly; tests that mutate state work on a ``shutil.copytree`` copy.

Fixture history (``main`` ends in a merge commit M)::

    c1 (Alice, 1700000000 +0000) "first"   a.txt=one,two  sub/b.txt=alpha,beta  long.txt=l1..l12
                                            crlf.txt=win\\r\\n,line\\r\\n  notes.md="see a.b here","axb"
                                            nonl.txt="last" (no newline)  dash.txt="-x","--y"
    c2 (Bob,   1700000100 +0000) "second"  a.txt=one,two,three
    d  (Dave,  1700000150 +0000) "gamma"   [branch feature from c2] sub/b.txt=alpha,beta,gamma
    c3 (Carol, 1700000200 +0530) "third"   a.txt=ONE,two,three  bin.dat=b"two\\x00\\x01zz\\n"
    M  (Alice, 1700000300 +0000)           merge feature into main (3-way, no conflicts)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 60
HEX40 = re.compile(r"^[0-9a-f]{40}$")
ZEROS = "0" * 40

ALICE = ("Alice", "alice@example.com")
BOB = ("Bob", "bob@example.com")
CAROL = ("Carol", "carol@example.com")
DAVE = ("Dave", "dave@example.com")

# Dates of the fixture commits as blame prints them (author timezone).
DATE_C1 = "2023-11-14 22:13:20 +0000"
DATE_C2 = "2023-11-14 22:15:00 +0000"
DATE_D = "2023-11-14 22:15:50 +0000"
DATE_C3 = "2023-11-15 03:46:40 +0530"


# ---------------------------------------------------------------------------
# Small driver helpers (nothing here imports the product)
# ---------------------------------------------------------------------------

class Result:
    def __init__(self, args, returncode, stdout, stderr):
        self.args = list(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    @property
    def lines(self) -> list[str]:
        return self.out.splitlines()

    def __str__(self) -> str:
        return (f"minigit {' '.join(self.args)} -> exit {self.returncode}\n"
                f"--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}")


def run(args, cwd, env, check=True) -> Result:
    """Run ``python -m minigit *args`` in ``cwd`` and capture bytes."""
    args = [str(a) for a in args]
    proc = subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd, env=env,
                          capture_output=True, timeout=TIMEOUT)
    res = Result(args, proc.returncode, proc.stdout, proc.stderr)
    if check and res.returncode != 0:
        raise AssertionError(f"command failed:\n{res}")
    return res


def make_env(home: str) -> dict:
    env = dict(os.environ)
    os.makedirs(home, exist_ok=True)
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(home, ".minigitconfig")
    env["MINIGIT_CREDENTIALS"] = os.path.join(home, ".minigit-credentials.json")
    env["PYTHONIOENCODING"] = "utf-8"
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = ROOT if not prev else ROOT + os.pathsep + prev
    for key in ("MINIGIT_USER", "MINIGIT_TOKEN", "MINIGIT_DEBUG", "MINIGIT_AUTHOR_NAME",
                "MINIGIT_AUTHOR_EMAIL", "MINIGIT_AUTHOR_DATE"):
        env.pop(key, None)
    return env


def as_author(env: dict, who: tuple[str, str], date: str) -> dict:
    e = dict(env)
    e["MINIGIT_AUTHOR_NAME"], e["MINIGIT_AUTHOR_EMAIL"] = who
    e["MINIGIT_AUTHOR_DATE"] = date
    return e


def write(path: str, data) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def build_fixture(wt: str, env: dict) -> dict:
    """Create the fixture repository in ``wt``; return the interesting shas."""
    run(["init"], wt, env)
    write(os.path.join(wt, "a.txt"), "one\ntwo\n")
    write(os.path.join(wt, "sub", "b.txt"), "alpha\nbeta\n")
    write(os.path.join(wt, "long.txt"), "".join(f"l{i}\n" for i in range(1, 13)))
    write(os.path.join(wt, "crlf.txt"), b"win\r\nline\r\n")
    write(os.path.join(wt, "notes.md"), "see a.b here\naxb\n")
    write(os.path.join(wt, "nonl.txt"), b"last")
    write(os.path.join(wt, "dash.txt"), "-x\n--y\n")
    run(["add", "."], wt, env)
    run(["commit", "-m", "first"], wt, as_author(env, ALICE, "1700000000 +0000"))

    write(os.path.join(wt, "a.txt"), "one\ntwo\nthree\n")
    run(["add", "a.txt"], wt, env)
    run(["commit", "-m", "second"], wt, as_author(env, BOB, "1700000100 +0000"))

    run(["switch", "-c", "feature"], wt, env)
    write(os.path.join(wt, "sub", "b.txt"), "alpha\nbeta\ngamma\n")
    run(["add", "sub/b.txt"], wt, env)
    run(["commit", "-m", "gamma"], wt, as_author(env, DAVE, "1700000150 +0000"))

    run(["switch", "main"], wt, env)
    write(os.path.join(wt, "a.txt"), "ONE\ntwo\nthree\n")
    write(os.path.join(wt, "bin.dat"), b"two\x00\x01zz\n")
    run(["add", "a.txt", "bin.dat"], wt, env)
    run(["commit", "-m", "third"], wt, as_author(env, CAROL, "1700000200 +0530"))

    res = run(["merge", "feature"], wt, as_author(env, ALICE, "1700000300 +0000"))
    assert "three-way" in res.out, str(res)

    def rev(spec: str) -> str:
        sha = run(["rev-parse", spec], wt, env).out.strip()
        assert HEX40.match(sha), sha
        return sha

    return {"c1": rev("HEAD~3"), "c2": rev("HEAD~2"), "c3": rev("HEAD~1"),
            "d": rev("HEAD^2"), "m": rev("HEAD")}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class BlameGrepAcceptance(unittest.TestCase):
    """blame + grep acceptance tests (features/blame-grep.md)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = os.path.realpath(cls._tmp.name)
        cls.env = make_env(os.path.join(cls.root, "home"))
        cls.fixture = os.path.join(cls.root, "fixture")
        os.makedirs(cls.fixture)
        cls.sha = build_fixture(cls.fixture, cls.env)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # -- helpers ------------------------------------------------------------
    def mg(self, *args, cwd=None, check=True, env=None) -> Result:
        return run(args, cwd=cwd or self.fixture, env=env or self.env, check=check)

    def fail_mg(self, *args, code=1, cwd=None) -> Result:
        res = self.mg(*args, cwd=cwd, check=False)
        self.assertEqual(res.returncode, code, str(res))
        return res

    def copy(self) -> str:
        """A private, mutable copy of the fixture repository."""
        dst = tempfile.mkdtemp(prefix="copy-", dir=self.root)
        shutil.rmtree(dst)
        shutil.copytree(self.fixture, dst)
        return dst

    def id8(self, key: str) -> str:
        return self.sha[key][:8]

    def boundary(self, key: str) -> str:
        return "^" + self.sha[key][:7]

    # ======================================================================
    # blame: human format
    # ======================================================================

    def test_blame_worktree_file_clean(self):
        """Default form blames the worktree file against HEAD; clean file shows no uncommitted lines."""
        res = self.mg("blame", "a.txt")
        self.assertEqual(res.lines, [
            f"{self.id8('c3')} (Carol {DATE_C3} 1) ONE",
            f"{self.boundary('c1')} (Alice {DATE_C1} 2) two",
            f"{self.id8('c2')} (Bob   {DATE_C2} 3) three",
        ], str(res))
        self.assertEqual(res.stderr, b"")

    def test_blame_follows_merge_to_side_branch_commit(self):
        """A line brought in by a merge is blamed on the side-branch commit, not on the merge."""
        res = self.mg("blame", "sub/b.txt")
        self.assertEqual(res.lines, [
            f"{self.boundary('c1')} (Alice {DATE_C1} 1) alpha",
            f"{self.boundary('c1')} (Alice {DATE_C1} 2) beta",
            f"{self.id8('d')} (Dave  {DATE_D} 3) gamma",
        ], str(res))
        self.assertNotIn(self.sha["m"][:7], res.out)

    def test_blame_at_revision_positional_and_dashdash(self):
        expected = [
            f"{self.boundary('c1')} (Alice {DATE_C1} 1) one",
            f"{self.boundary('c1')} (Alice {DATE_C1} 2) two",
            f"{self.id8('c2')} (Bob   {DATE_C2} 3) three",
        ]
        self.assertEqual(self.mg("blame", "HEAD~2", "a.txt").lines, expected)
        self.assertEqual(self.mg("blame", self.sha["c2"], "a.txt").lines, expected)
        self.assertEqual(self.mg("blame", "HEAD~2", "--", "a.txt").lines, expected)

    def test_blame_at_root_commit_is_all_boundary(self):
        res = self.mg("blame", "HEAD~3", "a.txt")
        self.assertEqual(res.lines, [
            f"{self.boundary('c1')} (Alice {DATE_C1} 1) one",
            f"{self.boundary('c1')} (Alice {DATE_C1} 2) two",
        ], str(res))

    def test_blame_line_number_column_is_right_justified(self):
        res = self.mg("blame", "-L", "9,11", "long.txt")
        b = self.boundary("c1")
        self.assertEqual(res.lines, [
            f"{b} (Alice {DATE_C1}  9) l9",
            f"{b} (Alice {DATE_C1} 10) l10",
            f"{b} (Alice {DATE_C1} 11) l11",
        ], str(res))

    def test_blame_L_forms(self):
        two = f"{self.boundary('c1')} (Alice {DATE_C1} 2) two"
        three = f"{self.id8('c2')} (Bob   {DATE_C2} 3) three"
        self.assertEqual(self.mg("blame", "-L", "2,3", "a.txt").lines, [two, three])
        self.assertEqual(self.mg("blame", "-L", "2,+2", "a.txt").lines, [two, three])
        self.assertEqual(self.mg("blame", "-L", "2,", "a.txt").lines, [two, three])
        # a single printed line is padded to its own author only
        three1 = f"{self.id8('c2')} (Bob {DATE_C2} 3) three"
        self.assertEqual(self.mg("blame", "-L", "3", "a.txt").lines, [three1])
        self.assertEqual(self.mg("blame", "-L", "3,3", "a.txt").lines, [three1])
        # END past the end is clamped, not an error
        self.assertEqual(self.mg("blame", "-L", "3,99", "a.txt").lines, [three1])

    def test_blame_L_errors(self):
        res = self.fail_mg("blame", "-L", "13", "long.txt")
        self.assertIn("error:", res.err)
        self.assertIn("has only 12 lines", res.err)
        self.assertEqual(res.stdout, b"")
        for bad in ("0,2", "3,2", "x", ",1", "2,+0", "2,+"):
            res = self.fail_mg("blame", "-L", bad, "a.txt", code=2)
            self.assertIn("-L", res.err, str(res))

    def test_blame_suppress_and_email(self):
        res = self.mg("blame", "-s", "a.txt")
        self.assertEqual(res.lines, [
            f"{self.id8('c3')} 1) ONE",
            f"{self.boundary('c1')} 2) two",
            f"{self.id8('c2')} 3) three",
        ], str(res))
        res = self.mg("blame", "-e", "-L", "3,3", "a.txt")
        self.assertEqual(res.lines, [f"{self.id8('c2')} (<bob@example.com> {DATE_C2} 3) three"])
        res = self.mg("blame", "-e", "-L", "1,2", "a.txt")
        # emails are padded to the longest printed email
        self.assertEqual(res.lines, [
            f"{self.id8('c3')} (<carol@example.com> {DATE_C3} 1) ONE",
            f"{self.boundary('c1')} (<alice@example.com> {DATE_C1} 2) two",
        ], str(res))

    def test_blame_is_byte_faithful(self):
        res = self.mg("blame", "crlf.txt")
        b = self.boundary("c1").encode()
        self.assertEqual(res.stdout, (
            b + f" (Alice {DATE_C1} 1) win\r\n".encode()
            + b + f" (Alice {DATE_C1} 2) line\r\n".encode()
        ))
        res = self.mg("blame", "nonl.txt")
        self.assertEqual(res.stdout, b + f" (Alice {DATE_C1} 1) last\n".encode())

    def test_blame_from_subdirectory(self):
        sub = os.path.join(self.fixture, "sub")
        res = self.mg("blame", "-L", "3", "b.txt", cwd=sub)
        self.assertEqual(res.lines, [f"{self.id8('d')} (Dave {DATE_D} 3) gamma"])
        res = self.mg("-C", "sub", "blame", "--porcelain", "-L", "3", "b.txt")
        self.assertIn("filename sub/b.txt", res.lines)

    # ======================================================================
    # blame: worktree changes and porcelain
    # ======================================================================

    def test_blame_uncommitted_lines(self):
        wt = self.copy()
        write(os.path.join(wt, "a.txt"), "ONE\ntwo\nthree\nfour\n")
        res = self.mg("blame", "a.txt", cwd=wt)
        self.assertEqual(len(res.lines), 4, str(res))
        # "Not Committed Yet" is the longest author, so the others are padded to 17 columns
        self.assertRegex(res.lines[0], rf"^{self.id8('c3')} \(Carol {{13}}{re.escape(DATE_C3)} 1\) ONE$")
        self.assertRegex(res.lines[1], rf"^\^{self.sha['c1'][:7]} \(Alice {{13}}{re.escape(DATE_C1)} 2\) two$")
        self.assertRegex(res.lines[2], rf"^{self.id8('c2')} \(Bob {{15}}{re.escape(DATE_C2)} 3\) three$")
        self.assertRegex(res.lines[3],
                         r"^00000000 \(Not Committed Yet \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4} 4\) four$")
        # staging the change does not make it committed
        self.mg("add", "a.txt", cwd=wt)
        res = self.mg("blame", "-L", "4", "a.txt", cwd=wt)
        self.assertEqual(len(res.lines), 1)
        self.assertTrue(res.lines[0].startswith("00000000 (Not Committed Yet "), str(res))
        # an explicit revision ignores the worktree
        res = self.mg("blame", "HEAD", "a.txt", cwd=wt)
        self.assertEqual(len(res.lines), 3, str(res))

    def test_blame_porcelain(self):
        res = self.mg("blame", "--porcelain", "-L", "2,3", "a.txt")
        self.assertEqual(res.lines, [
            f"{self.sha['c1']} 2 2",
            "author Alice",
            "author-mail <alice@example.com>",
            "author-time 1700000000",
            "author-tz +0000",
            "committer Alice",
            "committer-mail <alice@example.com>",
            "committer-time 1700000000",
            "committer-tz +0000",
            "summary first",
            "boundary",
            "filename a.txt",
            "\ttwo",
            f"{self.sha['c2']} 3 3",
            "author Bob",
            "author-mail <bob@example.com>",
            "author-time 1700000100",
            "author-tz +0000",
            "committer Bob",
            "committer-mail <bob@example.com>",
            "committer-time 1700000100",
            "committer-tz +0000",
            "summary second",
            "filename a.txt",
            "\tthree",
        ], str(res))

    def test_blame_porcelain_timezone_and_orig_lineno(self):
        res = self.mg("blame", "--porcelain", "-L", "1,1", "a.txt")
        self.assertEqual(res.lines[0], f"{self.sha['c3']} 1 1")
        self.assertIn("author-time 1700000200", res.lines)
        self.assertIn("author-tz +0530", res.lines)
        self.assertIn("summary third", res.lines)
        self.assertNotIn("boundary", res.lines)
        self.assertEqual(res.lines[-1], "\tONE")

    def test_blame_porcelain_uncommitted(self):
        wt = self.copy()
        write(os.path.join(wt, "a.txt"), "ONE\ntwo\nthree\nfour\n")
        res = self.mg("blame", "--porcelain", "-L", "4", "a.txt", cwd=wt)
        self.assertEqual(res.lines[0], f"{ZEROS} 4 4")
        self.assertIn("author Not Committed Yet", res.lines)
        self.assertIn("author-mail <not.committed.yet>", res.lines)
        self.assertIn("summary Version of a.txt from a.txt", res.lines)
        self.assertEqual(res.lines[-1], "\tfour")

    # ======================================================================
    # blame: errors
    # ======================================================================

    def test_blame_errors(self):
        res = self.fail_mg("blame", "missing.txt")
        self.assertIn("does not exist in 'HEAD'", res.err)
        res = self.fail_mg("blame", "sub")                 # a directory
        self.assertIn("does not exist in 'HEAD'", res.err)
        res = self.fail_mg("blame", "HEAD~2", "bin.dat")   # not in that commit
        self.assertIn("does not exist in 'HEAD~2'", res.err)
        res = self.fail_mg("blame", "bin.dat")
        self.assertIn("binary", res.err)
        res = self.fail_mg("blame", "nosuchrev", "a.txt")
        self.assertIn("unknown revision", res.err)
        # usage
        self.fail_mg("blame", code=2)
        self.fail_mg("blame", "HEAD", "a.txt", "sub/b.txt", code=2)
        self.fail_mg("blame", "--", "a.txt", "sub/b.txt", code=2)

    def test_blame_untracked_and_unborn(self):
        wt = self.copy()
        write(os.path.join(wt, "new.txt"), "x\n")
        res = self.fail_mg("blame", "new.txt", cwd=wt)
        self.assertIn("does not exist in 'HEAD'", res.err)
        self.mg("add", "new.txt", cwd=wt)                   # staged but never committed
        res = self.fail_mg("blame", "new.txt", cwd=wt)
        self.assertIn("does not exist in 'HEAD'", res.err)

        empty = tempfile.mkdtemp(prefix="empty-", dir=self.root)
        self.mg("init", cwd=empty)
        write(os.path.join(empty, "a.txt"), "x\n")
        res = self.fail_mg("blame", "a.txt", cwd=empty)
        self.assertIn("does not have any commits yet", res.err)

    # ======================================================================
    # grep: sources and output
    # ======================================================================

    def test_grep_default_output_sorted_with_binary_notice(self):
        res = self.mg("grep", "two")
        self.assertEqual(res.lines, ["a.txt:two", "Binary file bin.dat matches"], str(res))
        self.assertEqual(res.stderr, b"")

    def test_grep_line_numbers(self):
        self.assertEqual(self.mg("grep", "-n", "three").lines, ["a.txt:3:three"])
        self.assertEqual(self.mg("grep", "-n", "a", "--", "sub").lines,
                         ["sub/b.txt:1:alpha", "sub/b.txt:2:beta", "sub/b.txt:3:gamma"])

    def test_grep_ignore_case_word_fixed(self):
        self.assertEqual(self.mg("grep", "-i", "one").lines, ["a.txt:ONE"])
        self.assertEqual(self.mg("grep", "-w", "two").lines, ["a.txt:two", "Binary file bin.dat matches"])
        res = self.fail_mg("grep", "-w", "tw")
        self.assertEqual((res.stdout, res.stderr), (b"", b""))
        self.assertEqual(self.mg("grep", "a.b").lines, ["notes.md:see a.b here", "notes.md:axb"])
        self.assertEqual(self.mg("grep", "-F", "a.b").lines, ["notes.md:see a.b here"])
        self.assertEqual(self.mg("grep", "-i", "-w", "-F", "one").lines, ["a.txt:ONE"])

    def test_grep_multiple_patterns(self):
        res = self.mg("grep", "-e", "ONE", "-e", "beta")
        self.assertEqual(res.lines, ["a.txt:ONE", "sub/b.txt:beta"])
        # with -e every positional is a revision or a path
        res = self.mg("grep", "-n", "-e", "^l1", "long.txt")
        self.assertEqual(res.lines, ["long.txt:1:l1", "long.txt:10:l10", "long.txt:11:l11",
                                     "long.txt:12:l12"])
        # a pattern starting with '-' is attached to -e with '='
        self.assertEqual(self.mg("grep", "-n", "-e=-x").lines, ["dash.txt:1:-x"])
        self.assertEqual(self.mg("grep", "-e=--y").lines, ["dash.txt:--y"])

    def test_grep_names_only_and_count(self):
        self.assertEqual(self.mg("grep", "-l", "a").lines, ["nonl.txt", "notes.md", "sub/b.txt"])
        self.assertEqual(self.mg("grep", "-c", "-i", "e").lines,
                         ["a.txt:2", "crlf.txt:1", "notes.md:1", "sub/b.txt:1"])
        self.assertEqual(self.mg("grep", "-l", "two").lines, ["a.txt", "bin.dat"])
        self.assertEqual(self.mg("grep", "-c", "two").lines, ["a.txt:1", "bin.dat:1"])
        # -n is ignored with -l / -c
        self.assertEqual(self.mg("grep", "-n", "-l", "gamma").lines, ["sub/b.txt"])

    def test_grep_at_revision_prefix_and_tree(self):
        res = self.mg("grep", "-n", "two", "HEAD~2")
        self.assertEqual(res.lines, ["HEAD~2:a.txt:2:two"])        # no bin.dat at c2
        res = self.fail_mg("grep", "ONE", "HEAD~2")                 # ONE only exists from c3 on
        self.assertEqual((res.stdout, res.stderr), (b"", b""))
        res = self.mg("grep", "-l", "gamma", "feature")
        self.assertEqual(res.lines, ["feature:sub/b.txt"])
        res = self.mg("grep", "-c", "gamma", self.sha["d"], "--", "sub")
        self.assertEqual(res.lines, [f"{self.sha['d']}:sub/b.txt:1"])

    def test_grep_pathspec_filtering(self):
        self.assertEqual(self.mg("grep", "a", "--", "sub/").lines,
                         ["sub/b.txt:alpha", "sub/b.txt:beta", "sub/b.txt:gamma"])
        self.assertEqual(self.mg("grep", "a", "sub").lines,          # without "--": an existing path
                         ["sub/b.txt:alpha", "sub/b.txt:beta", "sub/b.txt:gamma"])
        self.assertEqual(self.mg("grep", "a", "--", "notes.md", "nonl.txt").lines,
                         ["nonl.txt:last", "notes.md:see a.b here", "notes.md:axb"])
        res = self.fail_mg("grep", "three", "--", "sub")            # nothing under sub matches
        self.assertEqual((res.stdout, res.stderr), (b"", b""))
        res = self.fail_mg("grep", "three", "--", "nosuchdir")      # unknown pathspec: no match, no error
        self.assertEqual((res.stdout, res.stderr), (b"", b""))

    def test_grep_is_byte_faithful(self):
        self.assertEqual(self.mg("grep", "win").stdout, b"crlf.txt:win\r\n")
        self.assertEqual(self.mg("grep", "last").stdout, b"nonl.txt:last\n")
        self.assertEqual(self.mg("grep", "-n", "line").stdout, b"crlf.txt:2:line\r\n")

    def test_grep_paths_are_repo_relative_from_subdirectory(self):
        sub = os.path.join(self.fixture, "sub")
        self.assertEqual(self.mg("grep", "alpha", cwd=sub).lines, ["sub/b.txt:alpha"])
        self.assertEqual(self.mg("-C", "sub", "grep", "-n", "beta").lines, ["sub/b.txt:2:beta"])
        # a relative pathspec is resolved against the cwd
        self.assertEqual(self.mg("grep", "-l", "a", "--", "b.txt", cwd=sub).lines, ["sub/b.txt"])

    # ======================================================================
    # grep: worktree vs index vs untracked
    # ======================================================================

    def test_grep_untracked_files_are_not_searched(self):
        wt = self.copy()
        write(os.path.join(wt, "untracked.txt"), "needle\n")
        res = self.fail_mg("grep", "needle", cwd=wt)
        self.assertEqual((res.stdout, res.stderr), (b"", b""))
        self.mg("add", "untracked.txt", cwd=wt)
        self.assertEqual(self.mg("grep", "needle", cwd=wt).lines, ["untracked.txt:needle"])

    def test_grep_cached_versus_worktree(self):
        wt = self.copy()
        write(os.path.join(wt, "a.txt"), "ONE\ntwo\nthree\nstagedline\n")
        self.mg("add", "a.txt", cwd=wt)
        write(os.path.join(wt, "a.txt"), "ONE\ntwo\nthree\nstagedline\nwtline\n")
        self.assertEqual(self.mg("grep", "-n", "stagedline", cwd=wt).lines, ["a.txt:4:stagedline"])
        self.assertEqual(self.mg("grep", "--cached", "-n", "stagedline", cwd=wt).lines,
                         ["a.txt:4:stagedline"])
        self.assertEqual(self.mg("grep", "wtline", cwd=wt).lines, ["a.txt:wtline"])
        res = self.fail_mg("grep", "--cached", "wtline", cwd=wt)
        self.assertEqual((res.stdout, res.stderr), (b"", b""))
        res = self.fail_mg("grep", "stagedline", "HEAD", cwd=wt)      # not committed
        self.assertEqual((res.stdout, res.stderr), (b"", b""))

    def test_grep_tracked_file_missing_from_disk_is_skipped(self):
        wt = self.copy()
        os.unlink(os.path.join(wt, "notes.md"))
        res = self.fail_mg("grep", "-F", "a.b", cwd=wt)
        self.assertEqual((res.stdout, res.stderr), (b"", b""))
        self.assertEqual(self.mg("grep", "--cached", "-F", "a.b", cwd=wt).lines, ["notes.md:see a.b here"])
        self.assertEqual(self.mg("grep", "-F", "a.b", "HEAD", cwd=wt).lines, ["HEAD:notes.md:see a.b here"])

    # ======================================================================
    # grep: exit codes and usage
    # ======================================================================

    def test_grep_no_match_exits_1_silently(self):
        res = self.fail_mg("grep", "zzz-not-there")
        self.assertEqual((res.stdout, res.stderr), (b"", b""))
        res = self.fail_mg("grep", "-l", "zzz-not-there")
        self.assertEqual((res.stdout, res.stderr), (b"", b""))
        res = self.fail_mg("grep", "-c", "zzz-not-there")
        self.assertEqual((res.stdout, res.stderr), (b"", b""))

    def test_grep_usage_errors(self):
        self.fail_mg("grep", code=2)
        res = self.fail_mg("grep", "(", code=2)
        self.assertIn("invalid regular expression", res.err)
        self.fail_mg("grep", "-l", "-c", "two", code=2)
        self.fail_mg("grep", "--cached", "two", "HEAD", code=2)
        self.fail_mg("grep", "two", "HEAD", "HEAD~1", code=2)
        res = self.fail_mg("grep", "two", "nosuchrev")
        self.assertIn("unknown revision", res.err)

    def test_help_mentions_both_commands(self):
        out = self.mg("--help").out
        self.assertIn("blame", out)
        self.assertIn("grep", out)
        self.assertIn("-L", self.mg("blame", "--help").out)
        self.assertIn("--cached", self.mg("grep", "--help").out)


class BlameGrepEdgeCases(unittest.TestCase):
    """Unit-level edge cases (module API and small CLI scenarios)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.env = make_env(os.path.join(self.root, "home"))
        self.wt = os.path.join(self.root, "wt")
        os.makedirs(self.wt)
        run(["init"], self.wt, self.env)

    def mg(self, *args, env=None, check=True, cwd=None):
        return run(args, cwd or self.wt, env or self.env, check=check)

    def commit(self, who, date, msg="m"):
        self.mg("add", ".")
        self.mg("commit", "-m", msg, env=as_author(self.env, who, date))

    def put(self, name, data):
        write(os.path.join(self.wt, name), data)

    # -- pure helpers ---------------------------------------------------------
    def test_parse_line_range_table(self):
        from minigit import blame
        from minigit.errors import UsageError
        self.assertEqual(blame.parse_line_range("3"), (3, None, False))
        self.assertEqual(blame.parse_line_range("3,5"), (3, 5, False))
        self.assertEqual(blame.parse_line_range("3,+2"), (3, 2, True))
        self.assertEqual(blame.parse_line_range("3,"), (3, None, False))
        for bad in ("", "0", "0,2", "3,2", "x", "0,", ",3", "2,+0", "2,+", "-1", "1,2,3"):
            with self.assertRaises(UsageError, msg=bad):
                blame.parse_line_range(bad)

    def test_format_date_iso(self):
        from minigit.blame import format_date_iso
        self.assertEqual(format_date_iso(1700000000, 0), "2023-11-14 22:13:20 +0000")
        self.assertEqual(format_date_iso(1700000200, 19800), "2023-11-15 03:46:40 +0530")
        self.assertEqual(format_date_iso(1700000000, -3600), "2023-11-14 21:13:20 -0100")

    def test_compile_patterns(self):
        from minigit.errors import UsageError
        from minigit.grep import GrepOptions, compile_patterns
        rx = compile_patterns(GrepOptions(("a.b",), fixed=True))
        self.assertTrue(rx.search(b"xa.by"))
        self.assertFalse(rx.search(b"axb"))
        rx = compile_patterns(GrepOptions(("foo", "bar"), word=True, ignore_case=True))
        self.assertTrue(rx.search(b"a BAR b"))
        self.assertFalse(rx.search(b"foobar"))
        with self.assertRaises(UsageError):
            compile_patterns(GrepOptions(("(",)))

    def test_grep_blob_binary_and_text(self):
        from minigit.grep import GrepOptions, compile_patterns, grep_blob
        rx = compile_patterns(GrepOptions(("x",)))
        bin_match = grep_blob("b", b"x\0x\nx\n", rx)[0]
        self.assertTrue(bin_match.binary)
        self.assertEqual(bin_match.count, 2)
        self.assertEqual(grep_blob("b", b"y\0\n", rx), [])
        m = grep_blob("t", b"a\r\nx\r\nx", rx)
        self.assertEqual([(g.lineno, g.line) for g in m], [(2, b"x\r"), (3, b"x")])
        self.assertEqual(grep_blob("e", b"", rx), [])

    # -- blame scenarios ------------------------------------------------------
    def _conflicting_branches(self):
        self.put("f.txt", "base\n")
        self.commit(ALICE, "1700000000 +0000", "base")
        self.mg("switch", "-c", "side")
        self.put("f.txt", "side\n")
        self.commit(BOB, "1700000100 +0000", "side")
        self.mg("switch", "main")
        self.put("f.txt", "main\n")
        self.commit(CAROL, "1700000200 +0000", "main")

    def test_file_added_in_merge_commit_is_blamed_on_the_merge(self):
        self._conflicting_branches()
        dave = as_author(self.env, DAVE, "1700000300 +0000")
        res = self.mg("merge", "side", check=False, env=dave)
        self.assertEqual(res.returncode, 1, str(res))
        self.put("f.txt", "main\nside\n")
        self.put("extra.txt", "only in merge\n")
        self.mg("add", ".")
        self.mg("commit", "-m", "merge", env=dave)
        out = self.mg("blame", "extra.txt").lines
        self.assertEqual(len(out), 1)
        self.assertRegex(out[0], r"^[0-9a-f]{8} \(Dave 2023-11-14 22:18:20 \+0000 1\) only in merge$")
        out = self.mg("blame", "f.txt").lines
        self.assertIn("(Carol", out[0])      # "main" survives from the first parent
        self.assertIn("(Bob", out[1])        # "side" is carried over from the second parent

    def test_empty_file(self):
        self.put("e.txt", b"")
        self.commit(ALICE, "1700000000 +0000")
        self.assertEqual(self.mg("blame", "e.txt").stdout, b"")
        res = self.mg("blame", "-L", "1", "e.txt", check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("has only 0 lines", res.err)
        res = self.mg("grep", "x", check=False)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (1, b"", b""))

    def test_crlf_edit_changes_ownership_of_the_line(self):
        self.put("c.txt", b"a\r\nb\r\n")
        self.commit(ALICE, "1700000000 +0000")
        self.put("c.txt", b"a\r\nb\n")                 # line 2 changed (CRLF -> LF)
        self.commit(BOB, "1700000100 +0000")
        out = self.mg("blame", "-s", "c.txt").stdout.split(b"\n")
        self.assertTrue(out[0].startswith(b"^"))
        self.assertTrue(out[0].endswith(b"1) a\r"))
        self.assertFalse(out[1].startswith(b"^"))

    def test_duplicate_lines_are_all_attributed(self):
        self.put("d.txt", "x\nx\nx\n")
        self.commit(ALICE, "1700000000 +0000")
        self.put("d.txt", "x\nx\nx\nx\n")
        self.commit(BOB, "1700000100 +0000")
        out = self.mg("blame", "-s", "d.txt").lines
        self.assertEqual(len(out), 4)
        self.assertEqual(sum(1 for ln in out if ln.startswith("^")), 3)

    def test_clock_skew_still_attributes_correctly(self):
        self.put("s.txt", "one\n")
        self.commit(ALICE, "1700001000 +0000")
        self.put("s.txt", "one\ntwo\n")
        self.commit(BOB, "1700000500 +0000")           # child dated before its parent
        self.put("s.txt", "one\ntwo\nthree\n")
        self.commit(CAROL, "1700002000 +0000")
        out = self.mg("blame", "s.txt").lines
        self.assertEqual([ln.split(" (")[1].split(" ")[0] for ln in out], ["Alice", "Bob", "Carol"])
        self.assertTrue(out[0].startswith("^"))

    def test_bare_repository_with_rev(self):
        from minigit.blame import blame_command
        from minigit.errors import MinigitError
        from minigit.repo import Repository
        self.put("a.txt", "one\ntwo\n")
        self.commit(ALICE, "1700000000 +0000")
        repo = Repository.open(self.wt)
        bare = Repository.init(os.path.join(self.root, "bare"), bare=True)[0]
        shutil.rmtree(bare.gpath("objects"))
        shutil.copytree(repo.gpath("objects"), bare.gpath("objects"))
        sha = repo.head_commit()
        out = blame_command(bare, self.root, path="a.txt", rev=sha, line_range=None,
                            porcelain=False, show_email=False, suppress=True)
        self.assertEqual(out, ("^%s 1) one\n^%s 2) two\n" % (sha[:7], sha[:7])).encode())
        with self.assertRaises(MinigitError):
            blame_command(bare, self.root, path="a.txt", rev=None, line_range=None,
                          porcelain=False, show_email=False, suppress=False)

    def test_worktree_file_deleted_from_disk(self):
        self.put("a.txt", "one\n")
        self.commit(ALICE, "1700000000 +0000")
        os.unlink(os.path.join(self.wt, "a.txt"))
        res = self.mg("blame", "a.txt", check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("does not exist in the working tree", res.err)
        self.assertEqual(len(self.mg("blame", "HEAD", "a.txt").lines), 1)

    def test_grep_on_unborn_repository(self):
        res = self.mg("grep", "x", check=False)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (1, b"", b""))

    def test_grep_conflicted_path_listed_once(self):
        self._conflicting_branches()
        self.mg("merge", "side", check=False)
        self.assertEqual(self.mg("grep", "-c", "=======").lines, ["f.txt:1"])


if __name__ == "__main__":
    unittest.main()

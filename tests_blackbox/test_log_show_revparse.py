"""Black-box tests for the "log-show-revparse" area of minigit.

Covers ``log``, ``log --oneline``, ``show``, revision syntax (``HEAD~n``,
``HEAD^``, ``^N``, branch names, full refs, short hashes, ``rev:path``,
``ORIG_HEAD``/``MERGE_HEAD``) and detached HEAD, driving the REAL CLI
(``python -m minigit``) through subprocess in temporary directories.

Run from the project root::

    python -m unittest tests_blackbox.test_log_show_revparse

One deterministic fixture repository is built once per module (fixed identity
and fixed author dates, so every sha is reproducible). Read-only tests use it
directly; tests that mutate state work on a ``shutil.copytree`` copy.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 60
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
ONELINE_RE = re.compile(r"^([0-9a-f]{7})(?: \(([^)]*)\))? (.*)$")

# Well known git blob ids for the fixture contents.
BLOB_A = "78981922613b2afb6025042ff6bd878ac1994e85"  # b"a\n"
BLOB_B = "61780798228d17af2d34fce4cfbdf35556832472"  # b"b\n"
BLOB_C = "f2ad6c76f0115a6ba5b00456a849810e7ec0af20"  # b"c\n"


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


def make_env(home: str, date: str = "1700000000 +0000") -> dict:
    env = dict(os.environ)
    os.makedirs(home, exist_ok=True)
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(home, ".minigitconfig")
    env["MINIGIT_CREDENTIALS"] = os.path.join(home, ".minigit-credentials.json")
    env["MINIGIT_AUTHOR_NAME"] = "Test User"
    env["MINIGIT_AUTHOR_EMAIL"] = "test@example.com"
    env["MINIGIT_AUTHOR_DATE"] = date
    env["PYTHONIOENCODING"] = "utf-8"
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = ROOT if not prev else ROOT + os.pathsep + prev
    for key in ("MINIGIT_USER", "MINIGIT_TOKEN", "MINIGIT_DEBUG"):
        env.pop(key, None)
    return env


def with_date(env: dict, date: str) -> dict:
    e = dict(env)
    e["MINIGIT_AUTHOR_DATE"] = date
    return e


def write(path: str, data) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def commit(repo: str, env: dict, date: str, *messages: str) -> str:
    """``commit -m ...`` with a fixed author date; returns the summary line."""
    args = ["commit"]
    for m in messages:
        args += ["-m", m]
    return run(args, repo, with_date(env, date)).out.strip()


def parse_oneline(line: str) -> tuple[str, list[str], str]:
    m = ONELINE_RE.match(line)
    if not m:
        raise AssertionError(f"not a --oneline line: {line!r}")
    labels = [x.strip() for x in m.group(2).split(",")] if m.group(2) else []
    return m.group(1), labels, m.group(3)


def rev_parse(repo: str, env: dict, *revs: str) -> list[str]:
    res = run(["rev-parse", *revs], repo, env)
    shas = res.lines
    if len(shas) != len(revs) or not all(HEX40.match(s) for s in shas):
        raise AssertionError(f"unexpected rev-parse output:\n{res}")
    return shas


def existing_prefixes(repo: str) -> set[str]:
    """Every 4-hex prefix that some object in ``repo`` already has."""
    out: set[str] = set()
    objdir = os.path.join(repo, ".minigit", "objects")
    for d in os.listdir(objdir):
        if len(d) != 2:
            continue
        for f in os.listdir(os.path.join(objdir, d)):
            if len(f) == 38:
                out.add(d + f[:2])
    return out


def blobs_with_prefix(prefix: str, count: int, salt: str) -> list[bytes]:
    """Brute-force ``count`` distinct blob contents whose sha starts with ``prefix``."""
    found: list[bytes] = []
    i = 0
    while len(found) < count:
        data = f"{salt} {i}\n".encode()
        if blob_sha(data).startswith(prefix):
            found.append(data)
        i += 1
    return found


# ---------------------------------------------------------------------------
# Module fixture: one deterministic repository
#
#   c1 "first"        a.txt = "a\n"                              1700000000 +0000
#   c2 "second"+body  a.txt = "b\n"                              1700000100 +0530
#   c3 "third"        + b.txt, dir/sub/deep.txt, dir/top.txt     1700000200 -0800
#   cf "feat commit"  (branch feature, from c2) + f.txt          1700000150 +0000
#   M  "merge feature" (main: c3 + cf)                           1700000300 +0000
#   c5 "fifth"        + nonl.txt (no newline), bin.dat, crlf.txt 1700000400 +0000
#   c6 "sixth"        - nonl.txt, ~bin.dat, ~crlf.txt            1700000500 +0000
#
# HEAD = c6 (main). feature = cf. ORIG_HEAD = c3 (written by the merge).
# ---------------------------------------------------------------------------

class Fixture:
    tmp: tempfile.TemporaryDirectory
    path: str
    env: dict
    sha: dict[str, str]


FIX: Fixture | None = None


def _build_fixture() -> Fixture:
    fx = Fixture()
    fx.tmp = tempfile.TemporaryDirectory(prefix="mg-lsr-", ignore_cleanup_errors=True)
    tmp = fx.tmp.name
    fx.env = make_env(os.path.join(tmp, "home"))
    fx.path = repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    env = fx.env
    run(["init"], repo, env)

    write(os.path.join(repo, "a.txt"), "a\n")
    run(["add", "a.txt"], repo, env)
    commit(repo, env, "1700000000 +0000", "first")

    write(os.path.join(repo, "a.txt"), "b\n")
    run(["add", "a.txt"], repo, env)
    commit(repo, env, "1700000100 +0530", "second", "body line")

    write(os.path.join(repo, "b.txt"), "c\n")
    write(os.path.join(repo, "dir", "sub", "deep.txt"), "deep\n")
    write(os.path.join(repo, "dir", "top.txt"), "top\n")
    run(["add", "."], repo, env)
    commit(repo, env, "1700000200 -0800", "third")

    run(["switch", "-c", "feature", "HEAD~1"], repo, env)
    write(os.path.join(repo, "f.txt"), "feat\n")
    run(["add", "f.txt"], repo, env)
    commit(repo, env, "1700000150 +0000", "feat commit")

    run(["switch", "main"], repo, env)
    run(["merge", "feature", "-m", "merge feature"], repo, with_date(env, "1700000300 +0000"))

    write(os.path.join(repo, "nonl.txt"), b"no newline")
    write(os.path.join(repo, "bin.dat"), b"bin\0ary\n")
    write(os.path.join(repo, "crlf.txt"), b"line1\r\nline2\r\n")
    run(["add", "."], repo, env)
    commit(repo, env, "1700000400 +0000", "fifth")

    os.remove(os.path.join(repo, "nonl.txt"))
    write(os.path.join(repo, "bin.dat"), b"bin\0ary2\n")
    write(os.path.join(repo, "crlf.txt"), b"line1\r\nline2\r\nline3\r\n")
    run(["add", "."], repo, env)
    commit(repo, env, "1700000500 +0000", "sixth")

    shas = rev_parse(repo, env, "HEAD", "HEAD~1", "HEAD~2", "HEAD~3", "HEAD~2^2", "HEAD~4", "HEAD~5")
    fx.sha = dict(zip(["c6", "c5", "M", "c3", "cf", "c2", "c1"], shas))
    if len(set(fx.sha.values())) != 7:
        raise AssertionError(f"fixture shas are not distinct: {fx.sha}")
    return fx


def setUpModule() -> None:
    global FIX
    FIX = _build_fixture()


def tearDownModule() -> None:
    global FIX
    if FIX is not None:
        FIX.tmp.cleanup()
        FIX = None


class Base(unittest.TestCase):
    """Access to the shared fixture plus per-test scratch directories."""

    @property
    def fx(self) -> Fixture:
        assert FIX is not None
        return FIX

    @property
    def repo(self) -> str:
        return self.fx.path

    @property
    def env(self) -> dict:
        return self.fx.env

    @property
    def S(self) -> dict[str, str]:
        return self.fx.sha

    def scratch(self) -> str:
        tmp = tempfile.TemporaryDirectory(prefix="mg-lsr-t-", ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def copy_fixture(self) -> str:
        dst = os.path.join(self.scratch(), "repo")
        shutil.copytree(self.repo, dst)
        return dst

    def fresh_repo(self, *init_args: str) -> tuple[str, dict]:
        tmp = self.scratch()
        env = make_env(os.path.join(tmp, "home"))
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        run(["init", *init_args], repo, env)
        return repo, env

    def head_file(self, repo: str) -> str:
        return read(os.path.join(repo, ".minigit", "HEAD")).decode()

    def assertFails(self, res: Result, *needles: str, code: int = 1) -> None:
        self.assertEqual(res.returncode, code, f"expected exit {code}:\n{res}")
        for n in needles:
            self.assertIn(n, res.err, f"missing {n!r} in stderr:\n{res}")

    def oneline(self, repo: str, env: dict, *args: str) -> list[tuple[str, list[str], str]]:
        res = run(["log", "--oneline", *args], repo, env)
        return [parse_oneline(l) for l in res.lines]


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------

class TestLog(Base):

    def test_full_log_exact_text(self):
        """The complete ``log`` output: order, decorations, Merge line, dates, indentation."""
        S = self.S
        expected = (
            f"commit {S['c6']} (HEAD -> main)\n"
            "Author: Test User <test@example.com>\n"
            "Date:   Tue Nov 14 22:21:40 2023 +0000\n"
            "\n"
            "    sixth\n"
            "\n"
            f"commit {S['c5']}\n"
            "Author: Test User <test@example.com>\n"
            "Date:   Tue Nov 14 22:20:00 2023 +0000\n"
            "\n"
            "    fifth\n"
            "\n"
            f"commit {S['M']}\n"
            f"Merge: {S['c3'][:7]} {S['cf'][:7]}\n"
            "Author: Test User <test@example.com>\n"
            "Date:   Tue Nov 14 22:18:20 2023 +0000\n"
            "\n"
            "    merge feature\n"
            "\n"
            f"commit {S['c3']}\n"
            "Author: Test User <test@example.com>\n"
            "Date:   Tue Nov 14 14:16:40 2023 -0800\n"
            "\n"
            "    third\n"
            "\n"
            f"commit {S['cf']} (feature)\n"
            "Author: Test User <test@example.com>\n"
            "Date:   Tue Nov 14 22:15:50 2023 +0000\n"
            "\n"
            "    feat commit\n"
            "\n"
            f"commit {S['c2']}\n"
            "Author: Test User <test@example.com>\n"
            "Date:   Wed Nov 15 03:45:00 2023 +0530\n"
            "\n"
            "    second\n"
            "\n"
            "    body line\n"
            "\n"
            f"commit {S['c1']}\n"
            "Author: Test User <test@example.com>\n"
            "Date:   Tue Nov 14 22:13:20 2023 +0000\n"
            "\n"
            "    first\n"
        )
        res = run(["log"], self.repo, self.env)
        # git prints blank body lines as four spaces; the spec is silent, accept both.
        actual = res.stdout.decode("utf-8").replace("\n    \n", "\n\n")
        self.assertEqual(actual, expected, str(res))

    def test_oneline_exact_lines(self):
        S = self.S
        expected = [
            f"{S['c6'][:7]} (HEAD -> main) sixth",
            f"{S['c5'][:7]} fifth",
            f"{S['M'][:7]} merge feature",
            f"{S['c3'][:7]} third",
            f"{S['cf'][:7]} (feature) feat commit",
            f"{S['c2'][:7]} second",
            f"{S['c1'][:7]} first",
        ]
        res = run(["log", "--oneline"], self.repo, self.env)
        self.assertEqual(res.lines, expected, str(res))
        self.assertNotIn("body line", res.out)      # only the subject
        self.assertNotIn("Merge:", res.out)

    def test_max_count(self):
        S = self.S
        one = run(["log", "--oneline", "-n", "1"], self.repo, self.env)
        self.assertEqual(one.lines, [f"{S['c6'][:7]} (HEAD -> main) sixth"])
        three = run(["log", "--oneline", "--max-count", "3"], self.repo, self.env)
        self.assertEqual([parse_oneline(l)[0] for l in three.lines],
                         [S["c6"][:7], S["c5"][:7], S["M"][:7]])
        big = run(["log", "--oneline", "-n", "100"], self.repo, self.env)
        self.assertEqual(len(big.lines), 7)
        zero = run(["log", "--oneline", "-n", "0"], self.repo, self.env)
        self.assertEqual(zero.stdout, b"", str(zero))
        full = run(["log", "-n", "2"], self.repo, self.env)
        self.assertEqual(full.out.count("\ncommit ") + full.out.startswith("commit "), 2)

    def test_start_points_union_and_dedup(self):
        S = self.S
        from_merge = self.oneline(self.repo, self.env, "HEAD~2")
        self.assertEqual([x[0] for x in from_merge],
                         [S["M"][:7], S["c3"][:7], S["cf"][:7], S["c2"][:7], S["c1"][:7]])
        feature = self.oneline(self.repo, self.env, "feature")
        self.assertEqual([x[0] for x in feature], [S["cf"][:7], S["c2"][:7], S["c1"][:7]])
        # several starting points are a union, printed once each, in topo order
        both = self.oneline(self.repo, self.env, "feature", "main")
        self.assertEqual([x[0] for x in both],
                         [S[k][:7] for k in ("c6", "c5", "M", "c3", "cf", "c2", "c1")])
        dup = self.oneline(self.repo, self.env, "HEAD", "HEAD", "main")
        self.assertEqual(len(dup), 7)
        self.assertEqual(len({x[0] for x in dup}), 7)
        limited = self.oneline(self.repo, self.env, "-n", "2", "HEAD~1")
        self.assertEqual([x[0] for x in limited], [S["c5"][:7], S["M"][:7]])
        # --all on a repo where everything is reachable from main adds nothing
        everything = self.oneline(self.repo, self.env, "--all")
        self.assertEqual([x[0] for x in everything], [x[0] for x in both])

    def test_bad_revisions_and_usage(self):
        S = self.S
        self.assertFails(run(["log", "nosuch"], self.repo, self.env, check=False),
                         "error: unknown revision 'nosuch'")
        self.assertFails(run(["log", "HEAD~6"], self.repo, self.env, check=False),
                         "error: unknown revision 'HEAD~6'")
        blob = rev_parse(self.repo, self.env, "HEAD:a.txt")[0]
        self.assertFails(run(["log", blob], self.repo, self.env, check=False),
                         f"error: '{blob}' is not a commit")
        tree = rev_parse(self.repo, self.env, "HEAD:dir")[0]
        self.assertFails(run(["log", "--oneline", tree], self.repo, self.env, check=False),
                         "is not a commit")
        # a short id of a blob is still "not a commit"
        self.assertFails(run(["log", blob[:8]], self.repo, self.env, check=False),
                         "is not a commit")
        self.assertEqual(run(["log", "-n", "abc"], self.repo, self.env, check=False).returncode, 2)
        self.assertEqual(run(["log", "--bogus"], self.repo, self.env, check=False).returncode, 2)
        del S

    def test_topological_order_beats_timestamps(self):
        """Clock skew and a merge whose second parent is newer than the merge itself."""
        repo, env = self.fresh_repo()
        write(os.path.join(repo, "f"), "1\n"); run(["add", "f"], repo, env)
        commit(repo, env, "1700009000 +0000", "A root newest-ts")
        write(os.path.join(repo, "f"), "2\n"); run(["add", "f"], repo, env)
        commit(repo, env, "1700001000 +0000", "B child oldest-ts")
        write(os.path.join(repo, "f"), "3\n"); run(["add", "f"], repo, env)
        commit(repo, env, "1700005000 +0000", "C grandchild")
        subjects = [x[2] for x in self.oneline(repo, env)]
        self.assertEqual(subjects, ["C grandchild", "B child oldest-ts", "A root newest-ts"])

        run(["switch", "-c", "side", "HEAD~2"], repo, env)
        write(os.path.join(repo, "s"), "s\n"); run(["add", "s"], repo, env)
        commit(repo, env, "1700099000 +0000", "S side newest")
        run(["switch", "main"], repo, env)
        # main alone never shows the side commit, --all orders it by time
        self.assertEqual([x[2] for x in self.oneline(repo, env)],
                         ["C grandchild", "B child oldest-ts", "A root newest-ts"])
        self.assertEqual([x[2] for x in self.oneline(repo, env, "--all")],
                         ["S side newest", "C grandchild", "B child oldest-ts", "A root newest-ts"])
        # merge commit older than its second parent: still printed first
        run(["merge", "side", "-m", "M merge"], repo, with_date(env, "1700050000 +0000"))
        got = self.oneline(repo, env)
        self.assertEqual([x[2] for x in got],
                         ["M merge", "S side newest", "C grandchild", "B child oldest-ts", "A root newest-ts"])
        c, s = rev_parse(repo, env, "HEAD^1", "HEAD^2")
        full = run(["log"], repo, env).out
        self.assertIn(f"\nMerge: {c[:7]} {s[:7]}\n", full)
        self.assertEqual(full.count("Merge: "), 1)

    def test_unborn_repository(self):
        repo, env = self.fresh_repo()
        msg = "error: your current branch 'main' does not have any commits yet"
        self.assertFails(run(["log"], repo, env, check=False), msg)
        self.assertFails(run(["log", "--oneline"], repo, env, check=False), msg)
        self.assertFails(run(["log", "--all"], repo, env, check=False), msg)
        self.assertFails(run(["show"], repo, env, check=False))
        self.assertFails(run(["rev-parse", "HEAD"], repo, env, check=False), "error: unknown revision 'HEAD'")
        other, env2 = self.fresh_repo("-b", "trunk")
        self.assertFails(run(["log"], other, env2, check=False),
                         "error: your current branch 'trunk' does not have any commits yet")

    def test_outside_a_repository(self):
        tmp = self.scratch()
        env = make_env(os.path.join(tmp, "home"))
        outside = os.path.join(tmp, "nowhere")
        os.makedirs(outside)
        for args in (["log"], ["show"], ["rev-parse", "HEAD"], ["log", "--oneline", "-n", "1"]):
            res = run(args, outside, env, check=False)
            self.assertEqual(res.returncode, 1, str(res))
            self.assertIn("error: not a minigit repository (or any parent): ", res.err)
            self.assertIn(os.path.basename(outside), res.err)

    def test_from_subdirectory_and_dash_C(self):
        sub = os.path.join(self.repo, "dir", "sub")
        root_lines = run(["log", "--oneline"], self.repo, self.env).lines
        self.assertEqual(run(["log", "--oneline"], sub, self.env).lines, root_lines)
        self.assertEqual(rev_parse(sub, self.env, "HEAD", "HEAD~1"), [self.S["c6"], self.S["c5"]])
        # rev:path is repository-root relative, as in git
        self.assertEqual(run(["show", "HEAD:dir/sub/deep.txt"], sub, self.env).stdout, b"deep\n")
        self.assertFails(run(["show", "HEAD:deep.txt"], sub, self.env, check=False),
                         "error: path 'deep.txt' does not exist in 'HEAD'")
        parent = os.path.dirname(self.repo)
        viac = run(["-C", os.path.basename(self.repo), "log", "--oneline", "-n", "1"], parent, self.env)
        self.assertEqual(viac.lines, root_lines[:1])

    def test_date_formatting_edge_offsets(self):
        repo = self.copy_fixture()
        run(["commit", "--allow-empty", "-m", "nepal"], repo, with_date(self.env, "1700000000 +0545"))
        run(["commit", "--allow-empty", "-m", "newfoundland"], repo, with_date(self.env, "1700000000 -0330"))
        run(["commit", "--allow-empty", "-m", "y2038"], repo, with_date(self.env, "2147483647 +0000"))
        dates = [l for l in run(["log", "-n", "3"], repo, self.env).lines if l.startswith("Date:")]
        self.assertEqual(dates, [
            "Date:   Tue Jan 19 03:14:07 2038 +0000",
            "Date:   Tue Nov 14 18:43:20 2023 -0330",
            "Date:   Wed Nov 15 03:58:20 2023 +0545",
        ])

    def test_utf8_message_and_author(self):
        repo = self.copy_fixture()
        env = dict(self.env)
        env["MINIGIT_AUTHOR_NAME"] = "Zoë Ünïcode"
        subject = "héllo wörld ✓ 日本"
        run(["commit", "--allow-empty", "-m", subject], repo, env)
        full = run(["log", "-n", "1"], repo, env).stdout
        self.assertIn("Author: Zoë Ünïcode <test@example.com>\n".encode("utf-8"), full)
        self.assertIn(f"\n    {subject}\n".encode("utf-8"), full)
        one = run(["log", "--oneline", "-n", "1"], repo, env).stdout
        self.assertTrue(one.endswith(f"(HEAD -> main) {subject}\n".encode("utf-8")), one)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

class TestShow(Base):

    def header(self, key: str, decor: str = "") -> str:
        return f"commit {self.S[key]}{decor}\n"

    def test_defaults_to_head_and_shows_diff(self):
        S = self.S
        default = run(["show"], self.repo, self.env).stdout
        explicit = run(["show", "HEAD"], self.repo, self.env).stdout
        self.assertEqual(default, explicit)
        text = explicit.decode("utf-8")
        self.assertTrue(text.startswith(self.header("c6", " (HEAD -> main)") +
                                        "Author: Test User <test@example.com>\n"
                                        "Date:   Tue Nov 14 22:21:40 2023 +0000\n\n    sixth\n\n"), text)
        nonl7 = blob_sha(b"no newline")[:7]
        old_bin7, new_bin7 = blob_sha(b"bin\0ary\n")[:7], blob_sha(b"bin\0ary2\n")[:7]
        old_crlf7 = blob_sha(b"line1\r\nline2\r\n")[:7]
        new_crlf7 = blob_sha(b"line1\r\nline2\r\nline3\r\n")[:7]
        expected_body = (
            "diff --git a/bin.dat b/bin.dat\n"
            f"index {old_bin7}..{new_bin7} 100644\n"
            "Binary files a/bin.dat and b/bin.dat differ\n"
            "diff --git a/crlf.txt b/crlf.txt\n"
            f"index {old_crlf7}..{new_crlf7} 100644\n"
            "--- a/crlf.txt\n"
            "+++ b/crlf.txt\n"
            "@@ -1,2 +1,3 @@\n"
            " line1\r\n"
            " line2\r\n"
            "+line3\r\n"
            "diff --git a/nonl.txt b/nonl.txt\n"
            "deleted file mode 100644\n"
            f"index {nonl7}..0000000\n"
            "--- a/nonl.txt\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-no newline\n"
            "\\ No newline at end of file\n"
        )
        self.assertTrue(text.endswith(expected_body), text)
        del S

    def test_new_files_binary_and_missing_newline(self):
        text = run(["show", "HEAD~1"], self.repo, self.env).stdout.decode("utf-8")
        self.assertTrue(text.startswith(self.header("c5")), text)
        bin7 = blob_sha(b"bin\0ary\n")[:7]
        crlf7 = blob_sha(b"line1\r\nline2\r\n")[:7]
        nonl7 = blob_sha(b"no newline")[:7]
        expected_body = (
            "diff --git a/bin.dat b/bin.dat\n"
            "new file mode 100644\n"
            f"index 0000000..{bin7}\n"
            "Binary files /dev/null and b/bin.dat differ\n"
            "diff --git a/crlf.txt b/crlf.txt\n"
            "new file mode 100644\n"
            f"index 0000000..{crlf7}\n"
            "--- /dev/null\n"
            "+++ b/crlf.txt\n"
            "@@ -0,0 +1,2 @@\n"
            "+line1\r\n"
            "+line2\r\n"
            "diff --git a/nonl.txt b/nonl.txt\n"
            "new file mode 100644\n"
            f"index 0000000..{nonl7}\n"
            "--- /dev/null\n"
            "+++ b/nonl.txt\n"
            "@@ -0,0 +1 @@\n"
            "+no newline\n"
            "\\ No newline at end of file\n"
        )
        self.assertTrue(text.endswith("\n    fifth\n\n" + expected_body), text)

    def test_modification_and_root_commit(self):
        second = run(["show", "HEAD~4"], self.repo, self.env).stdout.decode("utf-8")
        self.assertTrue(second.startswith(self.header("c2")), second)
        self.assertTrue(second.endswith(
            "    second\n\n    body line\n\n"
            "diff --git a/a.txt b/a.txt\n"
            f"index {BLOB_A[:7]}..{BLOB_B[:7]} 100644\n"
            "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"), second)
        root = run(["show", "HEAD~5"], self.repo, self.env).stdout.decode("utf-8")
        self.assertTrue(root.startswith(self.header("c1")), root)
        self.assertTrue(root.endswith(
            "    first\n\n"
            "diff --git a/a.txt b/a.txt\nnew file mode 100644\n"
            f"index 0000000..{BLOB_A[:7]}\n--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1 @@\n+a\n"), root)
        third = run(["show", self.S["c3"]], self.repo, self.env).stdout.decode("utf-8")
        self.assertEqual([l for l in third.splitlines() if l.startswith("diff --git")], [
            "diff --git a/b.txt b/b.txt",
            "diff --git a/dir/sub/deep.txt b/dir/sub/deep.txt",
            "diff --git a/dir/top.txt b/dir/top.txt",
        ])
        self.assertIn(f"index 0000000..{BLOB_C[:7]}\n--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1 @@\n+c\n", third)

    def test_merge_commit_header_only(self):
        S = self.S
        res = run(["show", "HEAD~2"], self.repo, self.env)
        self.assertEqual(res.out, (
            f"commit {S['M']}\n"
            f"Merge: {S['c3'][:7]} {S['cf'][:7]}\n"
            "Author: Test User <test@example.com>\n"
            "Date:   Tue Nov 14 22:18:20 2023 +0000\n"
            "\n"
            "    merge feature\n"), str(res))

    def test_blob_bytes_are_raw(self):
        S = self.S
        cases = {
            "HEAD:a.txt": b"b\n",
            "HEAD~5:a.txt": b"a\n",
            "HEAD~1:nonl.txt": b"no newline",
            "HEAD:crlf.txt": b"line1\r\nline2\r\nline3\r\n",
            "HEAD~1:bin.dat": b"bin\0ary\n",
            "HEAD:dir/sub/deep.txt": b"deep\n",
            "feature:f.txt": b"feat\n",
            f"{S['c2']}:a.txt": b"b\n",
            BLOB_A: b"a\n",
            BLOB_C[:6]: b"c\n",
        }
        for spec, want in cases.items():
            with self.subTest(spec=spec):
                self.assertEqual(run(["show", spec], self.repo, self.env).stdout, want)

    def test_tree_listing(self):
        top_sha = rev_parse(self.repo, self.env, "HEAD:dir/top.txt")[0]
        sub_sha = rev_parse(self.repo, self.env, "HEAD:dir/sub")[0]
        listing = run(["show", "HEAD:dir"], self.repo, self.env).out
        self.assertEqual(listing.splitlines(), [
            f"040000 tree {sub_sha}\tsub",
            f"100644 blob {top_sha}\ttop.txt",
        ])
        # the same tree through other spellings
        tree_sha = rev_parse(self.repo, self.env, "HEAD:dir")[0]
        for spec in ("HEAD:dir/", tree_sha, tree_sha[:10], "main:dir", "HEAD~1:dir"):
            with self.subTest(spec=spec):
                self.assertEqual(run(["show", spec], self.repo, self.env).out, listing)
        root = run(["show", "HEAD:"], self.repo, self.env).out
        self.assertEqual(run(["show", "HEAD:."], self.repo, self.env).out, root)
        names = [l.split("\t", 1)[1] for l in root.splitlines()]
        self.assertEqual(names, ["a.txt", "b.txt", "bin.dat", "crlf.txt", "dir", "f.txt"])
        self.assertIn(f"100644 blob {BLOB_B}\ta.txt", root.splitlines())
        self.assertIn(f"040000 tree {tree_sha}\tdir", root.splitlines())

    def test_path_and_usage_errors(self):
        self.assertFails(run(["show", "HEAD:nosuch"], self.repo, self.env, check=False),
                         "error: path 'nosuch' does not exist in 'HEAD'")
        self.assertFails(run(["show", "HEAD~5:b.txt"], self.repo, self.env, check=False),
                         "error: path 'b.txt' does not exist in 'HEAD~5'")
        self.assertFails(run(["show", "HEAD:dir/top.txt/x"], self.repo, self.env, check=False),
                         "error: path 'dir/top.txt/x' does not exist in 'HEAD'")
        self.assertFails(run(["show", "HEAD:dir/nope/deep.txt"], self.repo, self.env, check=False),
                         "does not exist in 'HEAD'")
        self.assertFails(run(["show", "nosuch:a.txt"], self.repo, self.env, check=False),
                         "error: unknown revision 'nosuch'")
        self.assertFails(run(["show", "HEAD~9"], self.repo, self.env, check=False),
                         "error: unknown revision 'HEAD~9'")
        self.assertEqual(run(["show", "HEAD", "HEAD~1"], self.repo, self.env, check=False).returncode, 2)

    def test_empty_tree_root_commit(self):
        repo, env = self.fresh_repo()
        run(["commit", "--allow-empty", "-m", "empty root"], repo, with_date(env, "0 +0000"))
        head = rev_parse(repo, env, "HEAD")[0]
        self.assertEqual(rev_parse(repo, env, "HEAD:")[0], EMPTY_TREE)
        self.assertEqual(run(["show", "HEAD:"], repo, env).stdout, b"")
        show = run(["show"], repo, env).out
        self.assertEqual(show, (
            f"commit {head} (HEAD -> main)\n"
            "Author: Test User <test@example.com>\n"
            "Date:   Thu Jan 1 00:00:00 1970 +0000\n"
            "\n"
            "    empty root\n"))
        self.assertEqual(run(["log", "--oneline"], repo, env).lines, [f"{head[:7]} (HEAD -> main) empty root"])
        # an empty commit on top of real history: header only, no diff
        run(["commit", "--allow-empty", "-m", "noop"], repo, env)
        noop = run(["show"], repo, env).out
        self.assertTrue(noop.endswith("\n    noop\n"), noop)
        self.assertNotIn("diff --git", noop)


# ---------------------------------------------------------------------------
# revision syntax
# ---------------------------------------------------------------------------

class TestRevParse(Base):

    def test_tilde_caret_and_chains(self):
        S = self.S
        cases = {
            "HEAD": S["c6"], "@": S["c6"], "main": S["c6"], "HEAD~0": S["c6"], "HEAD^0": S["c6"],
            "main^0~0^0": S["c6"],
            "HEAD~1": S["c5"], "HEAD^": S["c5"], "HEAD^1": S["c5"], "HEAD~": S["c5"], "@~1": S["c5"],
            "main~1": S["c5"], "HEAD~1^0": S["c5"],
            "HEAD~2": S["M"], "HEAD^^": S["M"], "HEAD~1~1": S["M"], "HEAD~~": S["M"], "HEAD^~": S["M"],
            "HEAD~3": S["c3"], "HEAD~2^": S["c3"], "HEAD~2^1": S["c3"], "HEAD^^^": S["c3"],
            "HEAD~2^2": S["cf"], "feature": S["cf"], "HEAD~2^2~0": S["cf"],
            "HEAD~4": S["c2"], "HEAD~2^2~1": S["c2"], "HEAD~2^2^": S["c2"], "feature~1": S["c2"],
            "HEAD~3^": S["c2"],
            "HEAD~5": S["c1"], "HEAD~1~1~1~1~1": S["c1"], "HEAD^^^^^": S["c1"], "feature~2": S["c1"],
            S["c6"]: S["c6"], f"{S['M']}^2": S["cf"],
        }
        specs = list(cases)
        got = rev_parse(self.repo, self.env, *specs)
        for spec, sha in zip(specs, got):
            with self.subTest(spec=spec):
                self.assertEqual(sha, cases[spec])
        # several arguments print one sha per line, in order
        self.assertEqual(run(["rev-parse", "HEAD~5", "HEAD"], self.repo, self.env).lines, [S["c1"], S["c6"]])

    def test_short_output_and_rev_path(self):
        S = self.S
        short = run(["rev-parse", "--short", "HEAD", "HEAD~2"], self.repo, self.env).lines
        self.assertEqual(short, [S["c6"][:7], S["M"][:7]])
        blob, parent_blob, root_blob = rev_parse(self.repo, self.env, "HEAD:a.txt", "HEAD~4:a.txt", "HEAD~5:a.txt")
        self.assertEqual((blob, parent_blob, root_blob), (BLOB_B, BLOB_B, BLOB_A))
        hashed = run(["hash-object", "a.txt"], self.repo, self.env).out.strip()
        self.assertEqual(hashed, BLOB_B)
        self.assertEqual(rev_parse(self.repo, self.env, "HEAD~2^2:f.txt", "feature:f.txt"),
                         [blob_sha(b"feat\n")] * 2)
        tree = rev_parse(self.repo, self.env, "HEAD:")[0]
        self.assertEqual(run(["cat-file", "-t", tree], self.repo, self.env).out.strip(), "tree")
        self.assertEqual(run(["cat-file", "-t", "HEAD:a.txt"], self.repo, self.env).out.strip(), "blob")
        self.assertEqual(run(["cat-file", "-t", "HEAD~2^2"], self.repo, self.env).out.strip(), "commit")

    def test_errors(self):
        S = self.S
        blob = rev_parse(self.repo, self.env, "HEAD:a.txt")[0]
        bad = {
            "HEAD~6": "error: unknown revision 'HEAD~6'",
            "HEAD^2": "error: unknown revision 'HEAD^2'",          # c6 has one parent
            "HEAD~2^3": "error: unknown revision 'HEAD~2^3'",      # the merge has two
            "HEAD~5^": "error: unknown revision 'HEAD~5^'",        # the root has none
            "HEAD~x": "error: unknown revision 'HEAD~x'",
            "nosuch": "error: unknown revision 'nosuch'",
            "nosuch~1": "error: unknown revision 'nosuch~1'",
            "0123456789012345678901234567890123456789": "error: unknown revision",
            "refs/heads/nosuch": "error: unknown revision",
            "": "error: unknown revision ''",
            f"{blob}~1": f"error: '{blob}' is not a commit",
            f"{blob}^0": f"error: '{blob}' is not a commit",
            "HEAD:nosuch": "error: path 'nosuch' does not exist in 'HEAD'",
            "ORIG_HEAD:zzz": "error: path 'zzz' does not exist in 'ORIG_HEAD'",
        }
        for spec, needle in bad.items():
            with self.subTest(spec=spec):
                res = run(["rev-parse", spec], self.repo, self.env, check=False)
                self.assertFails(res, needle)
                self.assertEqual(res.stdout, b"")
        # a failing argument after a good one: the good one was printed, exit is 1
        res = run(["rev-parse", "HEAD", "nosuch"], self.repo, self.env, check=False)
        self.assertEqual(res.returncode, 1)
        self.assertEqual(res.lines, [S["c6"]])
        self.assertEqual(run(["rev-parse"], self.repo, self.env, check=False).returncode, 2)

    def test_names_and_full_ref_names(self):
        S = self.S
        got = rev_parse(self.repo, self.env, "main", "refs/heads/main", "feature", "refs/heads/feature",
                        "refs/heads/main~1", "refs/heads/feature^", "ORIG_HEAD")
        self.assertEqual(got, [S["c6"], S["c6"], S["cf"], S["cf"], S["c5"], S["c2"], S["c3"]])
        # the merge wrote ORIG_HEAD = the pre-merge head; it works as a log start too
        self.assertEqual(self.oneline(self.repo, self.env, "-n", "1", "ORIG_HEAD")[0][2], "third")
        self.assertFails(run(["rev-parse", "MERGE_HEAD"], self.repo, self.env, check=False),
                         "error: unknown revision 'MERGE_HEAD'")

    def test_short_hashes(self):
        S = self.S
        head = S["c6"]
        for n in (4, 5, 7, 12, 20, 39):
            with self.subTest(n=n):
                self.assertEqual(rev_parse(self.repo, self.env, head[:n])[0], head)
        self.assertEqual(rev_parse(self.repo, self.env, head[:8].upper())[0], head)
        self.assertEqual(rev_parse(self.repo, self.env, head.upper())[0], head)
        self.assertEqual(rev_parse(self.repo, self.env, head[:7] + "~1")[0], S["c5"])
        self.assertEqual(rev_parse(self.repo, self.env, S["M"][:6] + "^2")[0], S["cf"])
        self.assertEqual(self.oneline(self.repo, self.env, "-n", "1", S["c3"][:7])[0][2], "third")
        self.assertEqual(run(["show", S["c2"][:5] + ":a.txt"], self.repo, self.env).stdout, b"b\n")
        self.assertFails(run(["rev-parse", head[:3]], self.repo, self.env, check=False),
                         f"error: unknown revision '{head[:3]}'")
        # a prefix matching nothing
        missing = "fffffffff" if not head.startswith("f") else "000000000"
        self.assertFails(run(["rev-parse", missing], self.repo, self.env, check=False),
                         f"error: unknown revision '{missing}'")
        # not hex at all
        self.assertFails(run(["rev-parse", "abcg"], self.repo, self.env, check=False),
                         "error: unknown revision 'abcg'")

    def test_ambiguous_short_id(self):
        repo = self.copy_fixture()
        taken = existing_prefixes(repo)
        prefix = next(p for p in ("cafe", "face", "fade", "bead", "deaf", "feed", "babe", "dace") if p not in taken)
        a, b = blobs_with_prefix(prefix, 2, "amb")
        write(os.path.join(repo, "amb0.txt"), a)
        write(os.path.join(repo, "amb1.txt"), b)
        sha_a = run(["hash-object", "-w", "amb0.txt"], repo, self.env).out.strip()
        sha_b = run(["hash-object", "-w", "amb1.txt"], repo, self.env).out.strip()
        self.assertEqual((sha_a, sha_b), (blob_sha(a), blob_sha(b)))
        msg = f"error: ambiguous short id '{prefix}' (matches 2 objects)"
        self.assertFails(run(["rev-parse", prefix], repo, self.env, check=False), msg)
        self.assertFails(run(["show", prefix], repo, self.env, check=False), msg)
        self.assertFails(run(["log", prefix], repo, self.env, check=False), msg)
        # the shortest prefix that tells them apart is unique again
        n = 4
        while sha_a[:n] == sha_b[:n]:
            n += 1
        self.assertEqual(rev_parse(repo, self.env, sha_a[:n])[0], sha_a)
        self.assertEqual(run(["show", sha_b[:n]], repo, self.env).stdout, b)
        self.assertEqual(run(["show", sha_a[:n].upper()], repo, self.env).stdout, a)

    def test_branch_name_beats_hex_prefix(self):
        S = self.S
        repo = self.copy_fixture()
        taken = existing_prefixes(repo)
        name = next(p for p in ("beef", "dead", "cafe", "face", "fade", "bead", "deaf", "feed") if p not in taken)
        (data,) = blobs_with_prefix(name, 1, "shadow")
        write(os.path.join(repo, "shadow.txt"), data)
        blob = run(["hash-object", "-w", "shadow.txt"], repo, self.env).out.strip()
        self.assertTrue(blob.startswith(name))
        # without a branch the hex prefix finds the blob ...
        self.assertEqual(rev_parse(repo, self.env, name)[0], blob)
        # ... a branch with that name wins over the object prefix
        run(["branch", name, "HEAD~1"], repo, self.env)
        self.assertEqual(rev_parse(repo, self.env, name, f"refs/heads/{name}", f"{name}~1"),
                         [S["c5"], S["c5"], S["M"]])
        self.assertEqual(run(["cat-file", "-t", name], repo, self.env).out.strip(), "commit")
        sha7, labels, subject = self.oneline(repo, self.env, "-n", "1", name)[0]
        self.assertEqual((sha7, labels, subject), (S["c5"][:7], [name], "fifth"))
        # checkout of that name switches branches instead of detaching at the blob/prefix
        res = run(["checkout", name], repo, self.env)
        self.assertEqual(res.out.strip(), f"Switched to branch '{name}'")
        self.assertEqual(self.head_file(repo), f"ref: refs/heads/{name}\n")
        # the blob is still reachable through its longer unique id
        self.assertEqual(run(["show", blob[:12]], repo, self.env).stdout, data)

    def test_branch_names_with_slashes(self):
        S = self.S
        repo = self.copy_fixture()
        run(["branch", "topic/deep/name", "HEAD~1"], repo, self.env)
        self.assertEqual(rev_parse(repo, self.env, "topic/deep/name", "refs/heads/topic/deep/name",
                                   "topic/deep/name~1", "topic/deep/name^0"),
                         [S["c5"], S["c5"], S["M"], S["c5"]])
        entries = self.oneline(repo, self.env, "topic/deep/name")
        self.assertEqual(entries[0], (S["c5"][:7], ["topic/deep/name"], "fifth"))
        self.assertEqual(len(entries), 6)
        self.assertEqual(run(["show", "topic/deep/name:a.txt"], repo, self.env).stdout, b"b\n")
        self.assertFails(run(["rev-parse", "topic/deep"], repo, self.env, check=False),
                         "error: unknown revision 'topic/deep'")

    def test_merge_head_during_a_conflict(self):
        S = self.S
        repo = self.copy_fixture()
        run(["switch", "-c", "conf", "HEAD~1"], repo, self.env)
        write(os.path.join(repo, "a.txt"), "X\n")
        run(["add", "a.txt"], repo, self.env)
        commit(repo, self.env, "1700000600 +0000", "conf a")
        run(["switch", "main"], repo, self.env)
        write(os.path.join(repo, "a.txt"), "Y\n")
        run(["add", "a.txt"], repo, self.env)
        commit(repo, self.env, "1700000700 +0000", "main a")
        main_head, conf = rev_parse(repo, self.env, "HEAD", "conf")
        res = run(["merge", "conf"], repo, self.env, check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("CONFLICT", res.out)
        self.assertEqual(rev_parse(repo, self.env, "MERGE_HEAD", "MERGE_HEAD^0", "MERGE_HEAD~1", "ORIG_HEAD"),
                         [conf, conf, S["c5"], main_head])
        self.assertEqual(self.oneline(repo, self.env, "-n", "1", "MERGE_HEAD")[0], (conf[:7], ["conf"], "conf a"))
        self.assertEqual(run(["show", "MERGE_HEAD:a.txt"], repo, self.env).stdout, b"X\n")
        self.assertEqual(run(["show", "HEAD:a.txt"], repo, self.env).stdout, b"Y\n")
        # HEAD itself has not moved and log does not include the other side yet
        self.assertEqual(rev_parse(repo, self.env, "HEAD")[0], main_head)
        self.assertNotIn("conf a", run(["log", "--oneline"], repo, self.env).out)
        run(["merge", "--abort"], repo, self.env)
        self.assertFails(run(["rev-parse", "MERGE_HEAD"], repo, self.env, check=False),
                         "error: unknown revision 'MERGE_HEAD'")
        self.assertEqual(rev_parse(repo, self.env, "HEAD")[0], main_head)

    def test_orig_head_after_reset(self):
        S = self.S
        repo = self.copy_fixture()
        run(["reset", "--hard", "HEAD~2"], repo, self.env)
        self.assertEqual(rev_parse(repo, self.env, "HEAD", "ORIG_HEAD", "ORIG_HEAD~1", "ORIG_HEAD^"),
                         [S["M"], S["c6"], S["c5"], S["c5"]])
        self.assertEqual(self.oneline(repo, self.env, "-n", "1", "ORIG_HEAD")[0], (S["c6"][:7], [], "sixth"))
        self.assertEqual(run(["show", "ORIG_HEAD:crlf.txt"], repo, self.env).stdout, b"line1\r\nline2\r\nline3\r\n")
        self.assertEqual([x[2] for x in self.oneline(repo, self.env)],
                         ["merge feature", "third", "feat commit", "second", "first"])
        run(["reset", "--hard", "ORIG_HEAD"], repo, self.env)
        self.assertEqual(rev_parse(repo, self.env, "HEAD", "ORIG_HEAD"), [S["c6"], S["M"]])

    def test_remote_tracking_names_from_ref_files_and_decoration_order(self):
        """``origin/x`` lookups through the documented refs/remotes layout, and the
        spec's decoration order ``(HEAD -> main, origin/main, feature)``."""
        S = self.S
        repo = self.copy_fixture()
        remotes = os.path.join(repo, ".minigit", "refs", "remotes", "origin")
        write(os.path.join(remotes, "main"), S["c6"] + "\n")
        write(os.path.join(remotes, "feature"), S["cf"] + "\n")
        self.assertEqual(rev_parse(repo, self.env, "origin/main", "refs/remotes/origin/main",
                                   "origin/feature", "origin/main~1", "origin/feature^"),
                         [S["c6"], S["c6"], S["cf"], S["c5"], S["c2"]])
        self.assertEqual(run(["show", "origin/feature:f.txt"], repo, self.env).stdout, b"feat\n")
        self.assertEqual([x[2] for x in self.oneline(repo, self.env, "origin/feature")],
                         ["feat commit", "second", "first"])
        cf_line = [l for l in run(["log", "--oneline"], repo, self.env).lines if l.startswith(S["cf"][:7])]
        # git lists remote-tracking refs before other local branches: "(origin/feature, feature)"
        self.assertEqual(cf_line, [f"{S['cf'][:7]} (origin/feature, feature) feat commit"])
        run(["branch", "extra"], repo, self.env)
        head_line = run(["log", "--oneline", "-n", "1"], repo, self.env).lines[0]
        self.assertEqual(head_line, f"{S['c6'][:7]} (HEAD -> main, origin/main, extra) sixth")
        full_first = run(["log", "-n", "1"], repo, self.env).lines[0]
        self.assertEqual(full_first, f"commit {S['c6']} (HEAD -> main, origin/main, extra)")

    def test_branch_named_HEAD_is_refused(self):
        """git: "'HEAD' is not a valid branch name". Allowing it makes HEAD ambiguous."""
        S = self.S
        repo = self.copy_fixture()
        res = run(["branch", "HEAD", "HEAD~1"], repo, self.env, check=False)
        self.assertNotEqual(res.returncode, 0, f"a branch literally named HEAD was created:\n{res}")
        self.assertFalse(os.path.exists(os.path.join(repo, ".minigit", "refs", "heads", "HEAD")))
        self.assertEqual(rev_parse(repo, self.env, "HEAD")[0], S["c6"])
        self.assertEqual(self.oneline(repo, self.env, "-n", "1")[0][1], ["HEAD -> main"])


# ---------------------------------------------------------------------------
# detached HEAD
# ---------------------------------------------------------------------------

class TestDetachedHead(Base):

    def test_checkout_sha_detaches(self):
        S = self.S
        repo = self.copy_fixture()
        res = run(["checkout", S["c5"]], repo, self.env)
        self.assertEqual(res.out.strip(), f"HEAD is now at {S['c5'][:7]} fifth")
        self.assertEqual(self.head_file(repo), S["c5"] + "\n")
        self.assertEqual(run(["status"], repo, self.env).lines[0], f"HEAD detached at {S['c5'][:7]}")
        self.assertEqual(rev_parse(repo, self.env, "HEAD", "@", "main", "HEAD~1"), [S["c5"], S["c5"], S["c6"], S["M"]])
        entries = self.oneline(repo, self.env)
        self.assertEqual(entries[0], (S["c5"][:7], ["HEAD"], "fifth"))
        self.assertEqual(len(entries), 6)
        self.assertEqual(run(["log", "-n", "1"], repo, self.env).lines[0], f"commit {S['c5']} (HEAD)")
        self.assertEqual(read(os.path.join(repo, "nonl.txt")), b"no newline")   # worktree at c5
        # --all still shows main's tip, decorated as a plain branch
        self.assertEqual(self.oneline(repo, self.env, "--all")[0], (S["c6"][:7], ["main"], "sixth"))
        back = run(["checkout", "main"], repo, self.env)
        self.assertEqual(back.out.strip(), "Switched to branch 'main'")
        self.assertEqual(self.head_file(repo), "ref: refs/heads/main\n")
        self.assertEqual(self.oneline(repo, self.env, "-n", "1")[0], (S["c6"][:7], ["HEAD -> main"], "sixth"))

    def test_detached_at_branch_tip_decoration(self):
        S = self.S
        repo = self.copy_fixture()
        res = run(["switch", "--detach", "main"], repo, self.env)
        self.assertEqual(res.out.strip(), f"HEAD is now at {S['c6'][:7]} sixth")
        self.assertEqual(self.head_file(repo), S["c6"] + "\n")
        self.assertEqual(run(["log", "--oneline", "-n", "1"], repo, self.env).lines,
                         [f"{S['c6'][:7]} (HEAD, main) sixth"])
        self.assertEqual(run(["log", "-n", "1"], repo, self.env).lines[0], f"commit {S['c6']} (HEAD, main)")
        self.assertTrue(run(["show"], repo, self.env).out.startswith(f"commit {S['c6']} (HEAD, main)\n"))
        self.assertEqual(run(["status"], repo, self.env).lines[0], f"HEAD detached at {S['c6'][:7]}")
        # detaching at a short sha and at HEAD itself
        run(["switch", "main"], repo, self.env)
        self.assertEqual(run(["checkout", S["cf"][:7]], repo, self.env).out.strip(),
                         f"HEAD is now at {S['cf'][:7]} feat commit")
        self.assertEqual(run(["log", "--oneline", "-n", "1"], repo, self.env).lines,
                         [f"{S['cf'][:7]} (HEAD, feature) feat commit"])
        run(["switch", "main"], repo, self.env)
        run(["switch", "--detach"], repo, self.env)
        self.assertEqual(self.head_file(repo), S["c6"] + "\n")

    def test_commit_while_detached(self):
        S = self.S
        repo = self.copy_fixture()
        run(["checkout", "HEAD~1"], repo, self.env)
        write(os.path.join(repo, "o.txt"), "orphan\n")
        run(["add", "o.txt"], repo, self.env)
        summary = commit(repo, self.env, "1700000900 +0000", "orphan")
        orphan = rev_parse(repo, self.env, "HEAD")[0]
        self.assertEqual(summary.splitlines()[0], f"[detached HEAD {orphan[:7]}] orphan")
        self.assertEqual(self.head_file(repo), orphan + "\n")
        self.assertEqual(rev_parse(repo, self.env, "HEAD~1", "main"), [S["c5"], S["c6"]])
        self.assertEqual(self.oneline(repo, self.env, "-n", "2"),
                         [(orphan[:7], ["HEAD"], "orphan"), (S["c5"][:7], [], "fifth")])
        everything = self.oneline(repo, self.env, "--all")
        self.assertEqual(everything[:2], [(orphan[:7], ["HEAD"], "orphan"), (S["c6"][:7], ["main"], "sixth")])
        self.assertEqual(len(everything), 8)
        self.assertTrue(run(["show"], repo, self.env).out.startswith(f"commit {orphan} (HEAD)\n"))
        # switching back leaves the orphan unreachable, but the object is still there
        run(["switch", "main"], repo, self.env)
        self.assertNotIn("orphan", run(["log", "--oneline"], repo, self.env).out)
        self.assertNotIn("orphan", run(["log", "--oneline", "--all"], repo, self.env).out)
        self.assertEqual(self.oneline(repo, self.env, "-n", "1", orphan)[0], (orphan[:7], [], "orphan"))
        self.assertEqual(self.oneline(repo, self.env, "-n", "1", orphan[:8])[0][2], "orphan")

    def test_reset_while_detached_moves_head_only(self):
        S = self.S
        repo = self.copy_fixture()
        run(["switch", "--detach", "HEAD"], repo, self.env)
        run(["reset", "--hard", "HEAD~1"], repo, self.env)
        self.assertEqual(self.head_file(repo), S["c5"] + "\n")
        self.assertEqual(rev_parse(repo, self.env, "HEAD", "main", "ORIG_HEAD"), [S["c5"], S["c6"], S["c6"]])
        self.assertEqual(run(["status"], repo, self.env).lines[0], f"HEAD detached at {S['c5'][:7]}")
        self.assertEqual(self.oneline(repo, self.env, "-n", "1", "ORIG_HEAD")[0], (S["c6"][:7], ["main"], "sixth"))
        self.assertEqual(self.oneline(repo, self.env, "-n", "1")[0], (S["c5"][:7], ["HEAD"], "fifth"))

    def test_switch_to_a_commit_requires_detach(self):
        """git refuses ``switch <sha>`` ("a branch is expected"); only --detach may detach."""
        S = self.S
        repo = self.copy_fixture()
        for target in (S["c5"], S["c5"][:7]):
            with self.subTest(target=target):
                res = run(["switch", target], repo, self.env, check=False)
                self.assertNotEqual(res.returncode, 0, f"switch silently detached HEAD:\n{res}")
                self.assertEqual(self.head_file(repo), "ref: refs/heads/main\n")


# ---------------------------------------------------------------------------
# remote-tracking refs through a real server
# ---------------------------------------------------------------------------

class TestWithServer(Base):

    def start_server(self, tmp: str, env: dict) -> str:
        srv = os.path.join(tmp, "srv")
        run(["server", "init", srv], tmp, env)
        token = run(["server", "add-user", srv, "alice"], tmp, env).out.strip()
        run(["server", "create-repo", srv, "proj"], tmp, env)
        ready = os.path.join(tmp, "ready.txt")
        log = open(os.path.join(tmp, "server.log"), "ab")
        self.addCleanup(log.close)
        proc = subprocess.Popen(
            [sys.executable, "-m", "minigit", "serve", "--root", srv, "--port", "0",
             "--ready-file", ready, "--quiet"],
            cwd=tmp, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

        def stop():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
        self.addCleanup(stop)
        deadline = time.monotonic() + 20
        line = ""
        while time.monotonic() < deadline and not line:
            if proc.poll() is not None:
                raise AssertionError("server exited early")
            try:
                line = read(ready).decode("ascii", "replace").strip()
            except OSError:
                line = ""
            if not line:
                time.sleep(0.05)
        if not line:
            raise AssertionError("server did not become ready")
        env["MINIGIT_USER"] = "alice"
        env["MINIGIT_TOKEN"] = token
        return "http://" + line

    def test_origin_refs_after_clone_and_push(self):
        tmp = self.scratch()
        env = make_env(os.path.join(tmp, "home"))
        url = self.start_server(tmp, env)
        run(["clone", url + "/proj", "work"], tmp, env)
        work = os.path.join(tmp, "work")
        write(os.path.join(work, "a.txt"), "a\n")
        run(["add", "a.txt"], work, env)
        commit(work, env, "1700000000 +0000", "c1")
        run(["push", "-u", "origin", "main"], work, env)
        c1 = rev_parse(work, env, "HEAD")[0]
        sha7, labels, subject = self.oneline(work, env, "-n", "1")[0]
        self.assertEqual((sha7, set(labels), subject), (c1[:7], {"HEAD -> main", "origin/main"}, "c1"))

        write(os.path.join(work, "a.txt"), "b\n")
        run(["add", "a.txt"], work, env)
        commit(work, env, "1700000100 +0000", "c2")
        run(["branch", "feat"], work, env)
        c2 = rev_parse(work, env, "HEAD")[0]
        entries = self.oneline(work, env)
        self.assertEqual([(e[0], set(e[1]), e[2]) for e in entries], [
            (c2[:7], {"HEAD -> main", "feat"}, "c2"),
            (c1[:7], {"origin/main"}, "c1"),
        ])
        self.assertEqual(rev_parse(work, env, "origin/main", "refs/remotes/origin/main", "HEAD~1", "main~1"), [c1] * 4)
        self.assertEqual([x[2] for x in self.oneline(work, env, "origin/main")], ["c1"])
        self.assertEqual(run(["show", "origin/main:a.txt"], work, env).stdout, b"a\n")
        self.assertEqual(run(["show", "HEAD:a.txt"], work, env).stdout, b"b\n")
        full = run(["log", "-n", "1", "origin/main"], work, env).lines[0]
        self.assertEqual(full, f"commit {c1} (origin/main)")
        self.assertFails(run(["rev-parse", "origin/nosuch"], work, env, check=False),
                         "error: unknown revision 'origin/nosuch'")
        # after pushing, the remote-tracking ref moves with the branch
        run(["push"], work, env)
        self.assertEqual(rev_parse(work, env, "origin/main")[0], c2)
        self.assertEqual(set(self.oneline(work, env, "-n", "1")[0][1]), {"HEAD -> main", "origin/main", "feat"})


if __name__ == "__main__":
    unittest.main()

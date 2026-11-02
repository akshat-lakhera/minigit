"""Failure behaviour: git's exit codes and messages, no tracebacks, damaged repositories, fast paths.

Covers SPEC section 0 "Exit codes" (0 / 1 / 128 fatal / 129 usage / 130 ctrl-C / 141 broken pipe),
the 'did you mean' suggestions, OS errors turned into 'fatal:' lines with a hint, the strict config
parser, corrupt refs and objects under fsck / gc, and the in-memory tree shortcuts behind status and
commit (same results, far fewer object reads).
"""
from __future__ import annotations

import contextlib
import errno
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest.mock import patch

from minigit import cli, ops, trees
from minigit.objects import ObjectStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(identity: bool = True, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MINIGIT_")}
    env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(tempfile.gettempdir(), "minigit-ux-no-such-global-config")
    if identity:
        env.update(MINIGIT_AUTHOR_NAME="Ann", MINIGIT_AUTHOR_EMAIL="ann@example.com",
                   MINIGIT_AUTHOR_DATE="1700000000 +0000")
    env.update(extra)
    return env


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tmp.name
        self.wt = os.path.join(self.tmp, "wt")
        os.makedirs(self.wt)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def mg(self, *args, code: int | None = 0, cwd: str | None = None, env: dict | None = None):
        p = subprocess.run([sys.executable, "-m", "minigit", "-C", cwd or self.wt, *args], cwd=ROOT,
                           capture_output=True, env=env or _env())
        out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        self.assertNotIn("Traceback", err, err)
        if code is not None:
            self.assertEqual(p.returncode, code, f"minigit {' '.join(args)}\n{out}\n{err}")
        return p.returncode, out, err

    def write(self, rel: str, data: bytes | str) -> None:
        path = os.path.join(self.wt, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)

    def make_repo(self, commits: int = 2) -> None:
        self.mg("init")
        for i in range(commits):
            self.write("a.txt", f"a {i}\n")
            self.write("sub/b.txt", f"b {i}\n")
            self.mg("add", ".")
            self.mg("commit", "-m", f"c{i}")

    def obj(self, sha: str) -> str:
        return os.path.join(self.wt, ".minigit", "objects", sha[:2], sha[2:])

    def rev(self, spec: str) -> str:
        return self.mg("rev-parse", spec)[1].strip()


class ExitCodeTests(Base):
    def test_unknown_command_suggests_like_git(self):
        rc, _, err = self.mg("stauts", code=1)
        self.assertEqual(err, "minigit: 'stauts' is not a minigit command. See 'minigit --help'.\n\n"
                              "The most similar command is\n\tstatus\n")

    def test_usage_error_is_129_with_option_hint(self):
        self.make_repo(1)
        _, _, err = self.mg("log", "--grpah", code=129)
        self.assertIn("usage: minigit log", err)
        self.assertIn("hint: did you mean '--graph'?", err)

    def test_fatal_errors_are_128(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        _, _, err = self.mg("status", cwd=outside, code=128)
        self.assertTrue(err.startswith("fatal: not a minigit repository"), err)
        self.make_repo(1)
        self.assertIn("fatal: pathspec 'nope' did not match any files", self.mg("add", "nope", code=128)[2])
        self.assertIn("fatal: unknown revision 'nosuch'", self.mg("log", "nosuch", code=128)[2])
        self.assertIn("fatal: cannot change to", self.mg("status", cwd=os.path.join(self.tmp, "gone"), code=128)[2])
        # plain refusals stay 'error:' / 1, as in git
        self.assertIn("nothing to commit", self.mg("commit", "-m", "x", code=1)[1])  # git: status on stdout, exit 1

    def test_mistyped_branch_gets_a_did_you_mean_hint(self):
        self.make_repo(1)
        self.mg("branch", "feature")
        _, _, err = self.mg("log", "featrue", code=128)
        self.assertEqual(err, "fatal: unknown revision 'featrue'\nhint: did you mean 'feature'?\n")
        self.assertEqual(self.mg("log", "zzzz", code=128)[2], "fatal: unknown revision 'zzzz'\n")

    def test_missing_identity_is_fatal_with_a_hint(self):
        self.mg("init")
        self.write("a.txt", "a\n")
        self.mg("add", "a.txt")
        _, _, err = self.mg("commit", "-m", "x", env=_env(identity=False), code=128)
        self.assertTrue(err.startswith("fatal: author identity unknown (user.name and user.email not set)\n"), err)
        self.assertIn('hint:   minigit config --global user.name "Your Name"\n', err)
        self.assertEqual(self.mg("status", "--porcelain")[1], "A  a.txt\n")  # nothing was committed

    def test_broken_pipe_is_silent(self):
        self.mg("init")
        self.write("big.txt", "line of text\n" * 200_000)  # 2.6 MB: far more than a pipe buffer
        self.mg("add", "big.txt")
        self.mg("commit", "-m", "big")
        p = subprocess.Popen([sys.executable, "-m", "minigit", "-C", self.wt, "cat-file", "-p", "HEAD:big.txt"], cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env())
        p.stdout.read(10)
        p.stdout.close()  # 'minigit cat-file -p HEAD:big.txt | head -c 10'
        err = p.stderr.read().decode("utf-8", "replace")
        p.stderr.close()
        p.wait(timeout=60)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("Exception ignored", err)
        self.assertEqual(err, "")
        self.assertEqual(p.returncode, 141)


class InProcessErrorTests(unittest.TestCase):
    """OSErrors and interrupts that escape a command become one 'fatal:' line plus a hint."""

    def run_cli(self, exc: BaseException) -> tuple[int, str]:
        err = io.StringIO()
        with patch.object(cli, "_COMMANDS", dict(cli._COMMANDS, status=lambda ns, cwd: (_ for _ in ()).throw(exc))):
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = cli.main(["status"])
        return rc, err.getvalue()

    def test_permission_error(self):
        rc, err = self.run_cli(PermissionError(errno.EACCES, "Permission denied", "C:/r/.minigit/index"))
        self.assertEqual(rc, 128)
        self.assertTrue(err.startswith("fatal: permission denied: 'C:/r/.minigit/index'\nhint: "), err)
        self.assertNotIn("internal error", err)

    def test_disk_full(self):
        rc, err = self.run_cli(OSError(errno.ENOSPC, "No space left on device", "x.tmp"))
        self.assertEqual(rc, 128)
        self.assertIn("fatal: no space left on device: 'x.tmp'", err)
        self.assertIn("hint: free some disk space", err)

    def test_keyboard_interrupt(self):
        rc, err = self.run_cli(KeyboardInterrupt())
        self.assertEqual((rc, err), (130, "interrupted\n"))

    def test_internal_error_has_a_hint_and_no_traceback(self):
        rc, err = self.run_cli(RuntimeError("kaboom"))
        self.assertEqual(rc, 128)
        self.assertIn("fatal: internal error: kaboom\nhint: this is a bug in minigit", err)
        self.assertNotIn("Traceback", err)

    def test_windows_einval_on_a_closed_pipe_counts_as_broken_pipe(self):
        class Dead(io.BytesIO):
            def write(self, data):
                raise OSError(errno.EINVAL, "Invalid argument")

        out = io.TextIOWrapper(Dead())
        with patch.object(cli, "_silence_stdout"), patch.object(sys, "stdout", out):
            with patch.object(cli, "_COMMANDS", dict(cli._COMMANDS, status=lambda ns, cwd: cli._out("x" * 10))):
                rc = cli.main(["status"])
        self.assertEqual(rc, 141)


class DamagedRepositoryTests(Base):
    def test_bad_config_line_is_reported_not_skipped(self):
        self.make_repo(1)
        with open(os.path.join(self.wt, ".minigit", "config"), "w") as f:
            f.write("[core\n\tbare = false\n")
        _, _, err = self.mg("status", code=128)
        self.assertIn("fatal: bad config line 1 in file ", err)
        self.assertIn("hint: fix or remove that line in a text editor: '[core'", err)

    def test_garbage_current_branch_is_not_treated_as_unborn(self):
        self.make_repo(2)
        ref = os.path.join(self.wt, ".minigit", "refs", "heads", "main")
        self.write("c.txt", "c\n")
        self.mg("add", "c.txt")
        with open(ref, "w") as f:
            f.write("not a sha\n")
        _, _, err = self.mg("commit", "-m", "would orphan the history", code=1)
        self.assertIn("error: refs/heads/main is corrupt", err)
        with open(ref) as f:
            self.assertEqual(f.read(), "not a sha\n")  # untouched
        rc, out, _ = self.mg("fsck", code=1)
        self.assertIn("ref refs/heads/main: 'not a sha' is not a commit id", out)
        self.assertIn("cannot prune: ref refs/heads/main", self.mg("gc", "--prune", "--force", code=1)[2])

    def test_garbage_other_branch_blocks_prune(self):
        self.make_repo(2)
        self.mg("branch", "side")
        objects_before = sum(len(f) for _d, _s, f in os.walk(os.path.join(self.wt, ".minigit", "objects")))
        with open(os.path.join(self.wt, ".minigit", "refs", "heads", "side"), "w") as f:
            f.write("garbage\n")
        _, _, err = self.mg("gc", "--prune", "--force", code=1)
        self.assertIn("cannot prune: ref refs/heads/side: 'garbage' is not a commit id", err)
        objects_after = sum(len(f) for _d, _s, f in os.walk(os.path.join(self.wt, ".minigit", "objects")))
        self.assertEqual(objects_before, objects_after)

    def test_corrupt_objects_under_fsck_gc_and_show(self):
        self.make_repo(2)
        blob = self.rev("HEAD:a.txt")
        tree = self.rev("HEAD:sub")
        os.chmod(self.obj(blob), 0o644)
        with open(self.obj(blob), "r+b") as f:  # truncated zlib stream
            data = f.read()
            f.seek(0)
            f.truncate()
            f.write(data[: len(data) // 2])
        rc, out, err = self.mg("fsck", code=1)
        self.assertIn(f"corrupt object {blob}: truncated zlib stream", out)
        self.assertIn("hint: copy a good version", err)
        _, _, err = self.mg("show", "HEAD", code=128)
        self.assertIn(f"fatal: object {blob} is corrupt: truncated zlib stream\nhint: run 'minigit fsck'", err)
        # wrong hash: valid zlib, other content
        os.chmod(self.obj(blob), 0o644)
        with open(self.obj(blob), "wb") as f:
            f.write(zlib.compress(b"blob 5\0evil\n"))
        self.assertIn(f"corrupt object {blob}: hash mismatch", self.mg("fsck", code=1)[1])
        # missing tree: fsck names it, gc --prune refuses and deletes nothing
        os.remove(self.obj(tree))
        self.assertIn(tree, self.mg("fsck", code=1)[1])
        before = sum(len(f) for _d, _s, f in os.walk(os.path.join(self.wt, ".minigit", "objects")))
        _, _, err = self.mg("gc", "--prune", "--force", code=1)
        self.assertIn("error: cannot prune: object ", err)
        self.assertIn("hint: nothing was deleted", err)
        self.assertEqual(before, sum(len(f) for _d, _s, f in os.walk(os.path.join(self.wt, ".minigit", "objects"))))


class FastPathTests(unittest.TestCase):
    """The in-memory tree hashing must give exactly what reading the trees gives."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.makedirs(os.path.join(self._tmp.name, "objects"))
        self.store = ObjectStore(os.path.join(self._tmp.name, "objects"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def blob(self, text: str) -> str:
        return self.store.write("blob", text.encode())

    def test_build_tree_matches_serialize_tree_order_and_ids(self):
        from minigit.objects import TreeEntry, serialize_tree

        b = self.blob("x")
        flat = {"a.txt": ("100644", b), "a/b": ("100755", b), "a-b": ("100644", b), "ü.txt": ("100644", b)}
        sub = self.store.write("tree", serialize_tree([TreeEntry("100755", "b", b)]))
        want = self.store.write("tree", serialize_tree([
            TreeEntry("100644", "a.txt", b), TreeEntry("40000", "a", sub), TreeEntry("100644", "a-b", b),
            TreeEntry("100644", "ü.txt", b)]))
        self.assertEqual(trees.build_tree(self.store, flat), want)

    def test_flatten_with_known_gives_the_same_result(self):
        b1, b2 = self.blob("1"), self.blob("2")
        head = {f"d{i}/s{j}/f{k}.txt": ("100644", b1) for i in range(3) for j in range(3) for k in range(3)}
        tree = trees.build_tree(self.store, head)
        variants = [dict(head), {}, dict(head, **{"d0/s0/f0.txt": ("100644", b2)}),
                    {k: v for k, v in head.items() if not k.startswith("d1/")},
                    dict(head, **{"new.txt": ("100644", b2)}), {"d0": ("100644", b1)}]
        for known in variants:
            with self.subTest(n=len(known)):
                fresh = ObjectStore(self.store.dir)
                self.assertEqual(trees.flatten_tree(fresh, tree, known), trees.flatten_tree(fresh, tree))

    def test_known_index_means_no_tree_reads(self):
        b = self.blob("1")
        head = {f"d{i}/f{k}.txt": ("100644", b) for i in range(5) for k in range(5)}
        tree = trees.build_tree(self.store, head)
        fresh = ObjectStore(self.store.dir)
        with patch.object(fresh, "read_raw", wraps=fresh.read_raw) as rr:
            self.assertEqual(trees.flatten_tree(fresh, tree, dict(head)), head)
            self.assertEqual(rr.call_count, 0)
            changed = dict(head, **{"d2/f0.txt": ("100644", self.blob("2"))})
            trees.flatten_tree(fresh, tree, changed)
            self.assertEqual(rr.call_count, 2)  # the root and d2 only


class StartupTests(unittest.TestCase):
    def test_status_builds_only_its_own_parser(self):
        p = cli.build_parser("status")
        self.assertEqual(sorted(p._minigit_sub.choices), ["status"])
        self.assertIn("stash", cli.build_parser()._minigit_sub.choices)

    def test_status_process_does_not_import_other_commands(self):
        code = ("import sys, runpy; sys.argv=['minigit','-C',sys.argv[1],'status'];\n"
                "try:\n runpy.run_module('minigit', run_name='__main__')\n"
                "except SystemExit: pass\n"
                "print(sorted(m for m in sys.modules if m in ('minigit.stash','minigit.pr','minigit.cherry',"
                "'minigit.gc','minigit.tags','minigit.serveradmin','random','datetime')))")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            ops.init(d)
            p = subprocess.run([sys.executable, "-c", code, d], cwd=ROOT, capture_output=True, env=_env(), text=True)
        self.assertEqual(p.stdout.strip().splitlines()[-1], "[]", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()

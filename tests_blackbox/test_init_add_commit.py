"""Black-box tests for the init / add / rm / commit area and object-store integrity.

Every test drives the real CLI (``python -m minigit``) as a subprocess inside a
fresh temporary directory, with an isolated HOME, global config and a fixed
identity + author date so commit ids are reproducible.  Nothing here imports
the ``minigit`` package: expectations come from SPEC.md and from how git
behaves, and the git object format is re-implemented independently so that
object ids are checked against an external oracle.

Run from the project root::

    python -m unittest tests_blackbox.test_init_add_commit
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 60

HEX7 = r"[0-9a-f]{7}"
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")

AUTHOR_NAME = "Test User"
AUTHOR_EMAIL = "test@example.com"
AUTHOR_DATE = "1700000000 +0000"

EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
HELLO_BLOB = "ce013625030ba8dba906f756967f9e9ca394464a"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- oracle
# An independent re-implementation of git's loose-object hashing (SPEC section 2).

def git_hash(kind: str, data: bytes) -> str:
    return hashlib.sha1(b"%s %d\0" % (kind.encode("ascii"), len(data)) + data).hexdigest()


def git_tree(entries: list[tuple[str, str, str]]) -> bytes:
    """``entries`` = [(mode, name, sha_hex)]; sorted the way git sorts them."""
    def key(e):
        mode, name, _sha = e
        return name.encode("utf-8") + (b"/" if mode == "40000" else b"")
    out = b""
    for mode, name, sha in sorted(entries, key=key):
        out += mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(sha)
    return out


def git_commit(tree: str, parents: list[str], message: str,
               name: str = AUTHOR_NAME, email: str = AUTHOR_EMAIL, date: str = AUTHOR_DATE) -> bytes:
    lines = [f"tree {tree}"]
    lines += [f"parent {p}" for p in parents]
    lines.append(f"author {name} <{email}> {date}")
    lines.append(f"committer {name} <{email}> {date}")
    if not message.endswith("\n"):
        message += "\n"
    return ("\n".join(lines) + "\n\n" + message).encode("utf-8")


# --------------------------------------------------------------------------- harness

@dataclass
class Result:
    args: list[str]
    rc: int
    stdout: bytes
    stderr: bytes

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")

    def __str__(self) -> str:
        return (f"minigit {' '.join(self.args)} -> exit {self.rc}\n"
                f"--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}")


def _popen(args, cwd, env):
    return subprocess.Popen([sys.executable, "-m", "minigit", *[str(a) for a in args]],
                            cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_cli(args, cwd, env, check=True) -> Result:
    args = [str(a) for a in args]
    proc = subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT)
    res = Result(args, proc.returncode, proc.stdout, proc.stderr)
    if check and res.rc != 0:
        raise AssertionError(f"command failed:\n{res}")
    return res


def write(path: str, data) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class Base(unittest.TestCase):
    """A temp dir with an isolated environment and an empty work tree ``self.wt``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.env = self.make_env()
        self.wt = os.path.join(self.root, "wt")
        os.makedirs(self.wt)

    def _cleanup(self):
        # Read-only files created by tests must not block the cleanup on Windows.
        for dirpath, _dirs, files in os.walk(self.root):
            for f in files:
                try:
                    os.chmod(os.path.join(dirpath, f), stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
        self.tmp.cleanup()

    def make_env(self, identity=True) -> dict:
        env = dict(os.environ)
        env["HOME"] = self.home
        env["USERPROFILE"] = self.home
        env["MINIGIT_GLOBAL_CONFIG"] = os.path.join(self.home, ".minigitconfig")
        env["MINIGIT_CREDENTIALS"] = os.path.join(self.home, ".minigit-credentials.json")
        env["PYTHONPATH"] = ROOT
        env["PYTHONIOENCODING"] = "utf-8"
        for key in ("MINIGIT_DEBUG", "MINIGIT_USER", "MINIGIT_TOKEN",
                    "MINIGIT_AUTHOR_NAME", "MINIGIT_AUTHOR_EMAIL", "MINIGIT_AUTHOR_DATE"):
            env.pop(key, None)
        if identity:
            env["MINIGIT_AUTHOR_NAME"] = AUTHOR_NAME
            env["MINIGIT_AUTHOR_EMAIL"] = AUTHOR_EMAIL
            env["MINIGIT_AUTHOR_DATE"] = AUTHOR_DATE
        return env

    # -- running ----------------------------------------------------------
    def mg(self, *args, cwd=None, env=None, check=True) -> Result:
        return run_cli(args, cwd=cwd or self.wt, env=env or self.env, check=check)

    def fail_mg(self, *args, code=1, cwd=None, env=None) -> Result:
        res = self.mg(*args, cwd=cwd, env=env, check=False)
        self.assertEqual(res.rc, code, str(res))
        return res

    # -- files ------------------------------------------------------------
    def path(self, rel: str) -> str:
        return os.path.join(self.wt, *rel.split("/"))

    def write(self, rel: str, data) -> None:
        write(self.path(rel), data)

    def read(self, rel: str) -> bytes:
        return read(self.path(rel))

    def exists(self, rel: str) -> bool:
        return os.path.lexists(self.path(rel))

    def gitfile(self, name: str) -> str:
        return read(os.path.join(self.wt, ".minigit", *name.split("/"))).decode("utf-8")

    def object_path(self, sha: str) -> str:
        return os.path.join(self.wt, ".minigit", "objects", sha[:2], sha[2:])

    def object_files(self) -> list[str]:
        out = []
        objdir = os.path.join(self.wt, ".minigit", "objects")
        for d in sorted(os.listdir(objdir)):
            sub = os.path.join(objdir, d)
            if os.path.isdir(sub):
                out += [d + n for n in sorted(os.listdir(sub))]
        return out

    def lock_files(self) -> list[str]:
        found = []
        for dirpath, _dirs, files in os.walk(os.path.join(self.wt, ".minigit")):
            found += [os.path.join(dirpath, f) for f in files if f.endswith(".lock")]
        return found

    # -- repo shortcuts ---------------------------------------------------
    def init(self, *args) -> Result:
        return self.mg("init", *args)

    def head(self) -> str:
        sha = self.mg("rev-parse", "HEAD").out.strip()
        self.assertRegex(sha, HEX40_RE)
        return sha

    def commit_file(self, rel: str, data, msg: str) -> str:
        self.write(rel, data)
        self.mg("add", rel)
        self.mg("commit", "-m", msg)
        return self.head()

    def porcelain(self) -> str:
        return self.mg("status", "--porcelain").out

    def ls_files(self) -> list[str]:
        return self.mg("ls-files").out.splitlines()

    def ls_stage(self) -> dict[str, tuple[str, str, str]]:
        """path -> (mode, sha, stage) from ``ls-files -s``."""
        out = {}
        for line in self.mg("ls-files", "-s").out.splitlines():
            meta, path = line.split("\t", 1)
            mode, sha, stage = meta.split(" ")
            out[path] = (mode, sha, stage)
        return out

    def tree_listing(self, spec: str) -> list[tuple[str, str, str, str]]:
        """[(mode, type, sha, name)] from ``cat-file -p <tree spec>``."""
        rows = []
        for line in self.mg("cat-file", "-p", spec).out.splitlines():
            meta, name = line.split("\t", 1)
            mode, typ, sha = meta.split(" ")
            rows.append((mode, typ, sha, name))
        return rows

    def commit_tree(self, rev: str = "HEAD") -> str:
        text = self.mg("cat-file", "-p", rev).out
        m = re.match(r"tree ([0-9a-f]{40})\n", text)
        self.assertIsNotNone(m, text)
        return m.group(1)


# =========================================================================== init

class InitTests(Base):
    def test_init_creates_layout_and_reports_path(self):
        res = self.init()
        gitdir = os.path.join(self.wt, ".minigit")
        self.assertEqual(res.out, f"Initialized empty minigit repository in {gitdir}\n")
        self.assertEqual(res.err, "")
        self.assertEqual(self.gitfile("HEAD"), "ref: refs/heads/main\n")
        cfg = self.gitfile("config")
        self.assertIn("[core]", cfg)
        self.assertRegex(cfg, r"\n\tbare = false\n")
        for d in ("objects", "refs/heads", "refs/remotes"):
            self.assertTrue(os.path.isdir(os.path.join(gitdir, *d.split("/"))), d)
        self.assertFalse(os.path.exists(os.path.join(gitdir, "index")), "index is absent until something is staged")
        self.assertEqual(self.ls_files(), [])
        status = self.mg("status").out
        self.assertEqual(status, "On branch main\n\nNo commits yet\n\nnothing to commit, working tree clean\n")
        self.assertEqual(self.porcelain(), "")
        res = self.fail_mg("log")
        self.assertIn("does not have any commits yet", res.err)

    def test_init_reinit_changes_nothing(self):
        self.init()
        sha = self.commit_file("a.txt", "a\n", "first")
        res = self.init("-b", "other")
        self.assertEqual(res.rc, 0)
        self.assertEqual(res.out, f"Reinitialized existing minigit repository in {os.path.join(self.wt, '.minigit')}\n")
        self.assertEqual(self.gitfile("HEAD"), "ref: refs/heads/main\n", "reinit must not move HEAD")
        self.assertEqual(self.gitfile("refs/heads/main"), sha + "\n")
        self.assertEqual(self.head(), sha)
        self.assertEqual(self.ls_files(), ["a.txt"])
        self.assertEqual(self.porcelain(), "")
        self.assertRegex(self.gitfile("config"), r"\tbare = false\n")
        # a plain re-run from a subdirectory reports reinit too and does not nest a repo
        os.makedirs(self.path("sub"))
        res = self.mg("init", cwd=self.path("sub"))
        self.assertIn("Initialized empty minigit repository", res.out)  # git nests a new repo here as well
        self.assertTrue(os.path.isdir(self.path("sub/.minigit")))

    def test_init_initial_branch_and_target_directory(self):
        target = os.path.join(self.root, "deep", "nested", "repo")
        res = self.mg("init", "-b", "dev", target, cwd=self.root)
        self.assertEqual(res.out, f"Initialized empty minigit repository in {os.path.join(target, '.minigit')}\n")
        self.assertEqual(read(os.path.join(target, ".minigit", "HEAD")), b"ref: refs/heads/dev\n")
        write(os.path.join(target, "f.txt"), "f\n")
        self.mg("add", "f.txt", cwd=target)
        res = self.mg("commit", "-m", "root on dev", cwd=target)
        self.assertRegex(res.out, rf"^\[dev \(root-commit\) {HEX7}\] root on dev\n( \d+ files? changed.*\n)?$")
        self.assertIn("On branch dev", self.mg("status", cwd=target).out)
        self.assertRegex(self.mg("log", "--oneline", cwd=target).out, rf"^{HEX7} \(HEAD -> dev\) root on dev\n$")
        # a relative directory argument resolves against cwd
        res = self.mg("init", "rel-repo", cwd=self.root)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "rel-repo", ".minigit", "HEAD")))

    def test_init_bare_repository(self):
        bare = os.path.join(self.root, "bare.git")
        res = self.mg("init", "--bare", bare, cwd=self.root)
        self.assertEqual(res.out, f"Initialized empty minigit repository in {bare}\n")
        self.assertEqual(read(os.path.join(bare, "HEAD")), b"ref: refs/heads/main\n")
        self.assertRegex(read(os.path.join(bare, "config")).decode(), r"\[core\]\n\tbare = true\n")
        self.assertTrue(os.path.isdir(os.path.join(bare, "objects")))
        self.assertTrue(os.path.isdir(os.path.join(bare, "refs", "heads")))
        self.assertFalse(os.path.exists(os.path.join(bare, ".minigit")))
        self.assertFalse(os.path.exists(os.path.join(bare, "index")))
        write(os.path.join(bare, "stray.txt"), "x\n")
        for args in (["status"], ["add", "stray.txt"], ["commit", "-m", "x"], ["rm", "stray.txt"], ["ls-files"]):
            res = self.fail_mg(*args, cwd=bare)
            self.assertIn("error: this operation must be run in a work tree", res.err, args)
        self.assertIn("Reinitialized existing", self.mg("init", "--bare", bare, cwd=self.root).out)

    def test_init_rejects_invalid_branch_names(self):
        for bad in ("a..b", "x y", "", "feat.lock", "a//b", "@{", "bad~name", "bad^name", "bad:name", ".hidden", "tab\tname"):
            with self.subTest(branch=bad):
                target = os.path.join(self.root, "repo-" + hashlib.md5(bad.encode()).hexdigest()[:6])
                res = self.mg("init", "-b", bad, target, cwd=self.root, check=False)
                self.assertEqual(res.rc, 1, str(res))
                self.assertTrue(res.err.startswith("error: "), str(res))
                self.assertFalse(os.path.exists(target), f"init must not create {target} for an invalid branch name")

    def test_init_on_a_file_path_is_a_clean_error(self):
        f = os.path.join(self.root, "afile")
        write(f, "not a dir\n")
        for target in (f, os.path.join(f, "sub")):
            with self.subTest(target=target):
                res = self.mg("init", target, cwd=self.root, check=False)
                self.assertEqual(res.rc, 1, str(res))
                self.assertEqual(res.out, "")
                self.assertTrue(res.err.startswith("error: "),
                                f"expected a user-facing 'error: ...' line, got:\n{res}")
                self.assertNotIn("internal error", res.err)
                self.assertNotIn("Traceback", res.err)
        self.assertEqual(read(f), b"not a dir\n")

    def test_commands_outside_a_repository(self):
        write(self.path("a.txt"), "a\n")
        for args in (["status"], ["add", "a.txt"], ["commit", "-m", "x"], ["rm", "a.txt"], ["ls-files"], ["log"]):
            with self.subTest(args=args):
                res = self.fail_mg(*args)
                self.assertEqual(res.out, "")
                self.assertIn("error: not a minigit repository (or any parent): ", res.err)
                self.assertIn(os.path.normcase(self.wt), os.path.normcase(res.err))
        # discovery walks up: a deep subdirectory of a repo is inside it
        self.init()
        deep = self.path("x/y/z")
        os.makedirs(deep)
        self.assertIn("On branch main", self.mg("status", cwd=deep).out)
        self.assertIn("On branch main", self.mg("-C", "x/y", "status").out)


# =========================================================================== add

class AddTests(Base):
    def setUp(self):
        super().setUp()
        self.init()

    def test_add_file_stages_the_exact_blob(self):
        self.write("a.txt", "hello\n")
        self.write("empty.txt", b"")
        res = self.mg("add", "a.txt", "empty.txt")
        self.assertEqual((res.out, res.err), ("", ""), "add is silent on success")
        stage = self.ls_stage()
        self.assertEqual(stage, {"a.txt": ("100644", HELLO_BLOB, "0"), "empty.txt": ("100644", EMPTY_BLOB, "0")})
        self.assertEqual(self.porcelain(), "A  a.txt\nA  empty.txt\n")
        status = self.mg("status").out
        self.assertIn("Changes to be committed:\n\tnew file:   a.txt\n\tnew file:   empty.txt\n", status)
        # the blob is a git-identical loose object
        raw = read(self.object_path(HELLO_BLOB))
        self.assertEqual(zlib.decompress(raw), b"blob 6\0hello\n")
        self.assertEqual(zlib.decompress(read(self.object_path(EMPTY_BLOB))), b"blob 0\0")
        self.assertEqual(self.mg("cat-file", "-p", HELLO_BLOB).stdout, b"hello\n")
        self.assertEqual(self.mg("cat-file", "-t", HELLO_BLOB).out, "blob\n")
        self.assertEqual(self.mg("cat-file", "-s", HELLO_BLOB).out, "6\n")
        self.assertEqual(self.mg("cat-file", "-s", EMPTY_BLOB).out, "0\n")
        # re-adding an unchanged file is a no-op; adding a modified file replaces the entry
        self.mg("add", "a.txt")
        self.assertEqual(self.ls_stage()["a.txt"][1], HELLO_BLOB)
        self.write("a.txt", "hello world\n")
        self.mg("add", "a.txt")
        self.assertEqual(self.ls_stage()["a.txt"][1], git_hash("blob", b"hello world\n"))
        self.assertEqual(self.ls_files(), ["a.txt", "empty.txt"], "no duplicate entries")

    def test_add_dot_and_directory_scope(self):
        self.write("Z.txt", "Z\n")
        self.write("a.txt", "a\n")
        self.write("sub/b.txt", "b\n")
        self.write("sub/deep/c.txt", "c\n")
        self.write("other/d.txt", "d\n")
        self.mg("add", "sub")
        self.assertEqual(self.ls_files(), ["sub/b.txt", "sub/deep/c.txt"])
        self.assertEqual(self.porcelain(), "?? Z.txt\n?? a.txt\n?? other/d.txt\nA  sub/b.txt\nA  sub/deep/c.txt\n")
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), ["Z.txt", "a.txt", "other/d.txt", "sub/b.txt", "sub/deep/c.txt"],
                         "ls-files is sorted bytewise, upper case first")
        self.assertEqual(self.porcelain(), "A  Z.txt\nA  a.txt\nA  other/d.txt\nA  sub/b.txt\nA  sub/deep/c.txt\n")
        self.assertFalse(any(p.startswith(".minigit") for p in self.ls_files()))
        # an empty directory adds nothing and is not an error (as in git)
        os.makedirs(self.path("emptydir"))
        res = self.mg("add", "emptydir")
        self.assertEqual(res.out, "")
        self.assertNotIn("emptydir", "\n".join(self.ls_files()))
        # a nested minigit repository's gitdir is never picked up, its files are
        os.makedirs(self.path("inner"))
        self.mg("init", cwd=self.path("inner"))
        self.write("inner/f.txt", "i\n")
        self.mg("add", ".")
        inner = [p for p in self.ls_files() if p.startswith("inner")]
        self.assertEqual(inner, ["inner/f.txt"])

    def test_add_normalizes_pathspecs_and_resolves_them_against_cwd(self):
        self.write("a.txt", "a\n")
        self.write("sub/b.txt", "b\n")
        specs = ["./a.txt", "sub/../a.txt", "sub/", "./sub/./b.txt", os.path.join(self.wt, "a.txt")]
        if IS_WINDOWS:
            specs.append("sub\\b.txt")
        for spec in specs:
            with self.subTest(spec=spec):
                self.mg("add", spec)
        self.assertEqual(self.ls_files(), ["a.txt", "sub/b.txt"], "every spelling maps to one POSIX repo path")
        self.assertEqual(self.porcelain(), "A  a.txt\nA  sub/b.txt\n")
        # the whole worktree given as an absolute path behaves like "."
        self.write("c.txt", "c\n")
        self.mg("add", self.wt)
        self.assertIn("c.txt", self.ls_files())
        # relative paths resolve against the directory the command runs in
        self.write("sub/c.txt", "c\n")
        self.write("other/x.txt", "x\n")
        sub = self.path("sub")
        self.mg("add", "c.txt", cwd=sub)
        self.assertIn("sub/c.txt", self.ls_files())
        self.assertNotIn("c.txt/", "\n".join(self.ls_files()))
        self.mg("add", os.path.join("..", "other", "x.txt"), cwd=sub)
        self.assertEqual(self.ls_files(), ["a.txt", "c.txt", "other/x.txt", "sub/b.txt", "sub/c.txt"])
        # porcelain paths are repo-relative wherever status runs
        self.assertEqual(self.mg("status", "--porcelain", cwd=sub).out,
                         "A  a.txt\nA  c.txt\nA  other/x.txt\nA  sub/b.txt\nA  sub/c.txt\n")
        res = self.mg("commit", "-m", "from sub", cwd=sub)
        self.assertRegex(res.out, rf"^\[main \(root-commit\) {HEX7}\] from sub\n( \d+ files? changed.*\n)?$")
        # adding an unchanged tracked file from its own directory is a silent no-op
        res = self.mg("add", "x.txt", cwd=self.path("other"))
        self.assertEqual((res.rc, res.out, res.err), (0, "", ""))
        self.assertEqual(self.porcelain(), "")
        res = self.mg("rm", "c.txt", cwd=sub)
        self.assertEqual(res.out, "rm 'sub/c.txt'\n")
        self.assertFalse(self.exists("sub/c.txt"))
        self.assertEqual(self.porcelain(), "D  sub/c.txt\n")
        # -C from outside the repo
        self.assertEqual(self.mg("-C", self.wt, "ls-files", cwd=self.root).out,
                         "a.txt\nc.txt\nother/x.txt\nsub/b.txt\n")

    def test_add_missing_tracked_paths_stage_deletions(self):
        for rel in ("a.txt", "sub/b.txt", "sub/deep/c.txt", "s2/f.txt"):
            self.write(rel, rel + "\n")
        self.mg("add", ".")
        self.mg("commit", "-m", "base")
        os.remove(self.path("a.txt"))
        self.assertEqual(self.porcelain(), " D a.txt\n")
        self.mg("add", "a.txt")  # missing but tracked: removed from the index
        self.assertEqual(self.porcelain(), "D  a.txt\n")
        import shutil
        shutil.rmtree(self.path("sub"))
        self.mg("add", "sub")  # the directory itself is gone
        self.assertEqual(self.porcelain(), "D  a.txt\nD  sub/b.txt\nD  sub/deep/c.txt\n")
        shutil.rmtree(self.path("s2"))
        self.mg("add", ".")
        self.assertEqual(self.porcelain(), "D  a.txt\nD  s2/f.txt\nD  sub/b.txt\nD  sub/deep/c.txt\n")
        self.assertEqual(self.ls_files(), [])
        res = self.mg("commit", "-m", "remove all")
        self.assertEqual(self.commit_tree(), EMPTY_TREE)
        self.assertEqual(self.porcelain(), "")

    def test_add_is_all_or_nothing(self):
        self.write("good.txt", "g\n")
        self.write("also.txt", "a\n")
        res = self.fail_mg("add", "good.txt", "nope", "also.txt")
        self.assertEqual(res.err, "error: pathspec 'nope' did not match any files\n")
        self.assertEqual(res.out, "")
        self.assertEqual(self.ls_files(), [], "a failed add must not stage anything")
        self.assertFalse(os.path.exists(self.path(".minigit/index")), "index must not be written")
        self.assertEqual(self.lock_files(), [])
        self.assertEqual(self.porcelain(), "?? also.txt\n?? good.txt\n")
        res = self.fail_mg("add", code=2)
        self.assertIn("error: ", res.err)
        self.assertEqual(self.ls_files(), [])
        self.mg("add", "good.txt", "also.txt")
        self.assertEqual(self.ls_files(), ["also.txt", "good.txt"])

    def test_add_refuses_paths_outside_and_the_gitdir(self):
        write(os.path.join(self.root, "outside.txt"), "o\n")
        self.write("a.txt", "a\n")
        for spec in ("../outside.txt", "..", os.path.join(self.root, "outside.txt")):
            with self.subTest(spec=spec):
                res = self.fail_mg("add", spec, "a.txt")
                self.assertRegex(res.err, r"^error: '.*' is outside repository\n$")
                self.assertEqual(self.ls_files(), [])
        for spec in (".minigit/HEAD", ".minigit", os.path.join(".minigit", "config"), "./.minigit/objects"):
            with self.subTest(spec=spec):
                res = self.fail_mg("add", spec)
                self.assertTrue(res.err.startswith("error: "), str(res))
                self.assertNotIn("internal error", res.err)
                self.assertEqual(self.ls_files(), [])
        self.assertEqual(self.gitfile("HEAD"), "ref: refs/heads/main\n")

    def test_add_force_cannot_stage_the_gitdir(self):
        # git: "pathspec '.git/...' did not match any files", with or without -f
        for spec in (".minigit/HEAD", os.path.join(".minigit", "config"), ".minigit"):
            with self.subTest(spec=spec):
                res = self.mg("add", "-f", spec, check=False)
                self.assertEqual(res.rc, 1, f"add -f on the gitdir must fail, not silently succeed:\n{res}")
                self.assertTrue(res.err.startswith("error: "), str(res))
                self.assertNotIn("internal error", res.err)
                self.assertEqual(self.ls_files(), [])

    def test_add_ignored_files_need_force(self):
        self.write(".minigitignore", "*.log\nbuild/\n!keep.log\n/rooted.txt\n")
        self.write("x.log", "x")
        self.write("keep.log", "k")
        self.write("build/out.txt", "o")
        self.write("rooted.txt", "r")
        self.write("sub/rooted.txt", "not anchored here")
        self.write("sub/.minigitignore", "secret\n")
        self.write("sub/secret", "s")
        self.write("sub/visible", "v")
        self.write("sub/deep/also.log", "l")
        self.assertEqual(self.porcelain(), "?? .minigitignore\n?? keep.log\n?? sub/.minigitignore\n"
                                           "?? sub/rooted.txt\n?? sub/visible\n")
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), [".minigitignore", "keep.log", "sub/.minigitignore",
                                           "sub/rooted.txt", "sub/visible"])
        for spec in ("x.log", "build/out.txt", "rooted.txt", "sub/secret", "sub/deep/also.log"):
            with self.subTest(spec=spec):
                res = self.fail_mg("add", spec)
                self.assertEqual(res.err, f"error: path '{spec}' is ignored by .minigitignore (use -f)\n")
        self.assertEqual(len(self.ls_files()), 5)
        self.mg("add", "-f", "x.log", "sub/secret")
        self.assertIn("x.log", self.ls_files())
        self.assertIn("sub/secret", self.ls_files())
        # a forced ignored file is now tracked: its modifications are picked up by "add ."
        self.mg("commit", "-m", "base")
        self.write("x.log", "changed")
        self.assertEqual(self.porcelain(), " M x.log\n")
        self.mg("add", "x.log")  # explicitly naming a tracked file needs no -f
        self.assertEqual(self.porcelain(), "M  x.log\n")
        self.write("sub/secret", "changed")
        self.mg("add", ".")
        self.assertEqual(self.porcelain(), "M  sub/secret\nM  x.log\n")
        os.remove(self.path("x.log"))
        self.mg("add", ".")
        self.assertEqual(self.porcelain(), "M  sub/secret\nD  x.log\n")

    def test_add_force_on_an_ignored_directory_stages_its_files(self):
        self.write(".minigitignore", "build/\n*.o\n")
        self.write("build/out.txt", "o\n")
        self.write("objs/a.o", "a\n")
        self.write("objs/b.o", "b\n")
        self.write("src/main.c", "m\n")
        res = self.fail_mg("add", "build")
        self.assertEqual(res.err, "error: path 'build' is ignored by .minigitignore (use -f)\n")
        res = self.mg("add", "-f", "build")
        self.assertEqual(res.rc, 0, str(res))
        self.assertIn("build/out.txt", self.ls_files(),
                      "add -f <ignored dir> must stage the files below it (git does); got %r" % self.ls_files())
        res = self.mg("add", "-f", "objs")
        self.assertEqual(res.rc, 0, str(res))
        self.assertEqual([p for p in self.ls_files() if p.startswith("objs/")], ["objs/a.o", "objs/b.o"],
                         "add -f <dir> must stage ignored files inside it")
        self.mg("add", "src")
        self.assertEqual(self.ls_files(), ["build/out.txt", "objs/a.o", "objs/b.o", "src/main.c"])

    def test_add_round_trips_bytes_exactly(self):
        import random
        rnd = random.Random(1234)
        big = bytes(rnd.getrandbits(8) for _ in range(300_000)) * 7  # ~2.1 MB, contains NULs
        cases = {
            "nul.bin": b"\x00\x01\x02\xff\x00end",
            "crlf.txt": b"line1\r\nline2\r\n\r\n",
            "cr.txt": b"old mac\rstyle\r",
            "noeol.txt": b"no trailing newline",
            "utf16.txt": "héllo".encode("utf-16"),
            "big.bin": big,
        }
        for rel, data in cases.items():
            self.write(rel, data)
        self.mg("add", ".")
        stage = self.ls_stage()
        for rel, data in cases.items():
            with self.subTest(rel=rel):
                sha = git_hash("blob", data)
                self.assertEqual(stage[rel], ("100644", sha, "0"))
                self.assertEqual(self.mg("cat-file", "-p", sha).stdout, data)
                self.assertEqual(self.mg("cat-file", "-s", sha).out, f"{len(data)}\n")
                self.assertEqual(zlib.decompress(read(self.object_path(sha))),
                                 b"blob %d\0" % len(data) + data)
        self.mg("commit", "-m", "bytes")
        for rel, data in cases.items():
            self.assertEqual(self.mg("cat-file", "-p", f"HEAD:{rel}").stdout, data, rel)
            self.assertEqual(self.mg("show", f"HEAD:{rel}").stdout, data, rel)
        self.assertEqual(self.porcelain(), "", "nothing may look modified after a commit")
        self.assertEqual(self.mg("diff").stdout, b"")

    def test_add_unicode_and_space_names(self):
        names = ["héllo wörld.txt", "日本語.txt", "emoji 🚀.md", "trailing space .txt", "a b/c d.txt"]
        for n in names:
            self.write(n, n + "\n")
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), sorted(names))
        for n in names:
            self.mg("add", n)  # by explicit name too
        self.assertEqual(len(self.ls_files()), len(names))
        res = self.mg("commit", "-m", "ünïcode ✓")
        self.assertRegex(res.out, rf"^\[main \(root-commit\) {HEX7}\] ünïcode ✓\n( \d+ files? changed.*\n)?$")
        for n in names:
            self.assertEqual(self.mg("cat-file", "-p", f"HEAD:{n}").stdout, (n + "\n").encode("utf-8"), n)
        self.assertEqual(self.porcelain(), "")
        self.assertEqual(self.mg("fsck").rc, 0)
        self.assertIn("ünïcode ✓", self.mg("log", "--oneline").out)
        rows = self.tree_listing("HEAD:")
        self.assertEqual({r[3] for r in rows}, {n for n in names if "/" not in n} | {"a b"})
        expected_order = sorted(rows, key=lambda r: r[3].encode("utf-8") + (b"/" if r[1] == "tree" else b""))
        self.assertEqual([r[3] for r in rows], [r[3] for r in expected_order], "tree entries in git byte order")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:a b/c d.txt").stdout, "a b/c d.txt\n".encode("utf-8"))

    def test_same_size_rewrite_is_not_missed_by_the_stat_cache(self):
        self.write("r.txt", "aaaa\n")
        self.mg("add", "r.txt")
        self.write("r.txt", "bbbb\n")  # same size, same second
        self.mg("add", ".")
        self.assertEqual(self.ls_stage()["r.txt"][1], git_hash("blob", b"bbbb\n"))
        self.assertEqual(self.porcelain(), "A  r.txt\n")
        self.mg("commit", "-m", "r")
        self.write("r.txt", "cccc\n")
        self.assertEqual(self.porcelain(), " M r.txt\n")
        self.mg("add", "r.txt")
        self.assertEqual(self.porcelain(), "M  r.txt\n")
        self.assertEqual(self.mg("cat-file", "-p", self.ls_stage()["r.txt"][1]).stdout, b"cccc\n")

    def test_file_replaced_by_directory_and_back(self):
        self.write("a.txt", "file\n")
        self.write("sub/b.txt", "b\n")
        self.mg("add", ".")
        self.mg("commit", "-m", "base")
        os.remove(self.path("a.txt"))
        self.write("a.txt/inner.txt", "inner\n")
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), ["a.txt/inner.txt", "sub/b.txt"])
        self.assertEqual(self.porcelain(), "D  a.txt\nA  a.txt/inner.txt\n")
        self.mg("commit", "-m", "file to dir")
        self.assertEqual(self.mg("fsck").rc, 0)
        self.assertEqual([(r[0], r[1], r[3]) for r in self.tree_listing("HEAD:")],
                         [("040000", "tree", "a.txt"), ("040000", "tree", "sub")])
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:a.txt/inner.txt").stdout, b"inner\n")
        import shutil
        shutil.rmtree(self.path("sub"))
        self.write("sub", "now a file\n")
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), ["a.txt/inner.txt", "sub"])
        self.mg("commit", "-m", "dir to file")
        self.assertEqual(self.mg("fsck").rc, 0)
        self.assertEqual([(r[1], r[3]) for r in self.tree_listing("HEAD:")], [("tree", "a.txt"), ("blob", "sub")])
        self.assertEqual(self.porcelain(), "")

    def test_add_reserved_names_fail_cleanly_without_touching_the_index(self):
        self.write("ok.txt", "ok\n")
        self.mg("add", "ok.txt")
        self.mg("commit", "-m", "ok")
        self.write("MINIGI~1", "alias of the gitdir on Windows\n")
        res = self.fail_mg("add", ".")
        self.assertTrue(res.err.startswith("error: "), str(res))
        self.assertNotIn("internal error", res.err)
        self.assertEqual(self.ls_files(), ["ok.txt"])
        res = self.fail_mg("add", "MINIGI~1")
        self.assertTrue(res.err.startswith("error: "), str(res))
        self.assertEqual(self.ls_files(), ["ok.txt"])
        self.assertEqual(self.lock_files(), [])
        self.assertIn("?? MINIGI~1\n", self.porcelain())
        self.write("ok.txt", "changed\n")
        self.mg("add", "ok.txt")  # unaffected paths still work
        self.assertEqual(self.porcelain(), "?? MINIGI~1\nM  ok.txt\n")

    def test_add_fails_when_the_index_is_locked(self):
        self.write("a.txt", "a\n")
        lock = self.path(".minigit/index.lock")
        write(lock, b"")
        res = self.fail_mg("add", "a.txt")
        self.assertIn("error: unable to lock", res.err)
        self.assertIn("index.lock exists", res.err)
        self.assertIn("If no other minigit process is running, delete it.", res.err)
        self.assertTrue(os.path.exists(lock), "a failing process must not delete somebody else's lock")
        self.assertEqual(self.ls_files(), [])
        os.remove(lock)
        self.mg("add", "a.txt")
        self.assertEqual(self.ls_files(), ["a.txt"])
        self.assertEqual(self.lock_files(), [])

    @unittest.skipUnless(IS_WINDOWS, "case-insensitive file system behaviour")
    def test_add_with_different_case_does_not_duplicate_an_entry_on_windows(self):
        self.write("sub/c.txt", "c\n")
        self.mg("add", "sub/c.txt")
        self.mg("add", "SUB/C.TXT")
        self.assertEqual(self.ls_files(), ["sub/c.txt"],
                         "one on-disk file must produce one index entry, with its on-disk spelling")
        self.mg("commit", "-m", "case")
        names = [r[3] for r in self.tree_listing("HEAD:")]
        self.assertEqual(names, ["sub"], "the tree must not contain both 'SUB' and 'sub'")
        self.assertEqual(self.porcelain(), "")

    def test_index_file_matches_the_documented_binary_format(self):
        files = {"b.txt": b"b\n", "a.txt": b"hello\n", "sub/c.txt": b"c\n", "日本.txt": b"j\n"}
        for rel, data in files.items():
            self.write(rel, data)
        self.mg("add", ".")
        data = read(self.path(".minigit/index"))
        header = struct.Struct(">4sII")
        entry = struct.Struct(">I20sQQBH")
        body, trailer = data[:-20], data[-20:]
        self.assertEqual(hashlib.sha1(body).digest(), trailer, "trailer is sha1 over the preceding bytes")
        magic, version, count = header.unpack_from(body, 0)
        self.assertEqual((magic, version, count), (b"MIDX", 1, len(files)))
        pos = header.size
        seen = []
        for _ in range(count):
            mode, sha, size, mtime_ns, stage, plen = entry.unpack_from(body, pos)
            pos += entry.size
            path = body[pos:pos + plen].decode("utf-8", "surrogateescape")
            pos += plen
            seen.append((path, stage))
            self.assertIn(mode, (0o100644, 0o100755))
            self.assertEqual(sha.hex(), git_hash("blob", files[path]), path)
            self.assertEqual(size, len(files[path]), path)
            self.assertGreater(mtime_ns, 0, path)
            self.assertEqual(stage, 0)
        self.assertEqual(pos, len(body), "no trailing bytes before the trailer")
        self.assertEqual(seen, sorted(seen), "entries sorted by (path, stage)")
        self.assertEqual([p for p, _ in seen], sorted(files))


# =========================================================================== rm

class RmTests(Base):
    def setUp(self):
        super().setUp()
        self.init()
        for rel in ("a.txt", "b.txt", "d.txt", "sub/c.txt", "sub/deep/e.txt"):
            self.write(rel, rel + "\n")
        self.mg("add", ".")
        self.mg("commit", "-m", "base")

    def test_rm_removes_file_and_index_entry(self):
        res = self.mg("rm", "a.txt")
        self.assertEqual(res.out, "rm 'a.txt'\n")
        self.assertEqual(res.err, "")
        self.assertFalse(self.exists("a.txt"))
        self.assertNotIn("a.txt", self.ls_files())
        self.assertEqual(self.porcelain(), "D  a.txt\n")
        self.assertIn("Changes to be committed:\n\tdeleted:    a.txt\n", self.mg("status").out)
        # a tracked file that is already gone from disk can still be rm'ed
        os.remove(self.path("b.txt"))
        self.assertEqual(self.porcelain(), "D  a.txt\n D b.txt\n")
        self.assertEqual(self.mg("rm", "b.txt").out, "rm 'b.txt'\n")
        self.assertEqual(self.porcelain(), "D  a.txt\nD  b.txt\n")
        self.mg("commit", "-m", "rm a b")
        res = self.fail_mg("cat-file", "-p", "HEAD:a.txt")
        self.assertEqual(res.err, "error: path 'a.txt' does not exist in 'HEAD'\n")
        self.assertEqual(sorted(r[3] for r in self.tree_listing("HEAD:")), ["d.txt", "sub"])
        self.assertEqual(self.porcelain(), "")
        # several paths at once, output sorted by path
        res = self.mg("rm", "sub/deep/e.txt", "d.txt")
        self.assertEqual(res.out, "rm 'd.txt'\nrm 'sub/deep/e.txt'\n")
        self.assertFalse(self.exists("sub/deep"), "empty directories are pruned")
        self.assertTrue(self.exists("sub/c.txt"))

    def test_rm_cached_keeps_the_file(self):
        res = self.mg("rm", "--cached", "d.txt")
        self.assertEqual(res.out, "rm 'd.txt'\n")
        self.assertEqual(self.read("d.txt"), b"d.txt\n")
        self.assertNotIn("d.txt", self.ls_files())
        self.assertEqual(self.porcelain(), "?? d.txt\nD  d.txt\n", "a staged deletion whose file remains shows D and ??")
        status = self.mg("status").out
        self.assertIn("\tdeleted:    d.txt\n", status)
        self.assertIn("Untracked files:\n\td.txt\n", status)
        # a newly staged (not yet committed) file: plain rm refuses, --cached is fine since I == W
        self.write("new.txt", "n\n")
        self.mg("add", "new.txt")
        res = self.fail_mg("rm", "new.txt")
        self.assertEqual(res.err, "error: 'new.txt' has staged or local changes (use -f)\n")
        self.assertTrue(self.exists("new.txt"))
        self.assertIn("new.txt", self.ls_files())
        self.assertEqual(self.mg("rm", "--cached", "new.txt").out, "rm 'new.txt'\n")
        self.assertTrue(self.exists("new.txt"))
        self.assertIn("?? new.txt\n", self.porcelain())
        # --cached -r on a directory keeps every file
        self.mg("rm", "--cached", "-r", "sub")
        self.assertTrue(self.exists("sub/c.txt") and self.exists("sub/deep/e.txt"))
        self.assertEqual(self.ls_files(), ["a.txt", "b.txt"])

    def test_rm_refuses_staged_or_local_changes_unless_forced(self):
        self.write("a.txt", "modified\n")
        res = self.fail_mg("rm", "a.txt")
        self.assertEqual(res.err, "error: 'a.txt' has staged or local changes (use -f)\n")
        self.assertEqual(self.read("a.txt"), b"modified\n")
        self.assertIn("a.txt", self.ls_files())
        # --cached is allowed when the index still matches HEAD (only the worktree changed)
        self.assertEqual(self.mg("rm", "--cached", "a.txt").out, "rm 'a.txt'\n")
        self.assertEqual(self.read("a.txt"), b"modified\n")
        # index matches neither HEAD nor the worktree: even --cached refuses
        self.write("b.txt", "staged\n")
        self.mg("add", "b.txt")
        self.write("b.txt", "and modified again\n")
        res = self.fail_mg("rm", "--cached", "b.txt")
        self.assertEqual(res.err, "error: 'b.txt' has staged or local changes (use -f)\n")
        res = self.fail_mg("rm", "b.txt")
        self.assertEqual(res.err, "error: 'b.txt' has staged or local changes (use -f)\n")
        self.assertEqual(self.read("b.txt"), b"and modified again\n")
        self.assertEqual(self.mg("rm", "-f", "b.txt").out, "rm 'b.txt'\n")
        self.assertFalse(self.exists("b.txt"))
        # staged but unchanged on disk: plain rm refuses (I != H), -f deletes
        self.write("d.txt", "v2\n")
        self.mg("add", "d.txt")
        self.fail_mg("rm", "d.txt")
        self.mg("rm", "-f", "d.txt")
        self.assertFalse(self.exists("d.txt"))
        self.assertEqual(self.porcelain(), "?? a.txt\nD  a.txt\nD  b.txt\nD  d.txt\n")

    def test_rm_directory_needs_recursive_and_prunes_empty_dirs(self):
        res = self.fail_mg("rm", "sub")
        self.assertEqual(res.err, "error: not removing 'sub' recursively without -r\n")
        self.assertTrue(self.exists("sub/c.txt") and self.exists("sub/deep/e.txt"))
        self.assertEqual(len(self.ls_files()), 5)
        res = self.fail_mg("rm", ".")
        self.assertEqual(res.err, "error: not removing '.' recursively without -r\n")
        self.assertEqual(len(self.ls_files()), 5)
        self.write("sub/untracked.txt", "u\n")
        # read-only files (a common Windows trap) are removed too
        ro = self.path("sub/deep/e.txt")
        os.chmod(ro, stat.S_IREAD)
        if IS_WINDOWS:
            subprocess.run(["attrib", "+R", ro], check=False)
        res = self.mg("rm", "-r", "sub")
        self.assertEqual(res.out, "rm 'sub/c.txt'\nrm 'sub/deep/e.txt'\n")
        self.assertFalse(self.exists("sub/c.txt"))
        self.assertFalse(self.exists("sub/deep"), "emptied directories are pruned")
        self.assertTrue(self.exists("sub/untracked.txt"), "untracked files survive")
        self.assertTrue(self.exists("sub"), "a directory with untracked content stays")
        self.assertEqual(self.porcelain(), "D  sub/c.txt\nD  sub/deep/e.txt\n?? sub/untracked.txt\n")
        self.mg("commit", "-m", "rm sub")
        self.assertEqual([r[3] for r in self.tree_listing("HEAD:")], ["a.txt", "b.txt", "d.txt"])
        res = self.mg("rm", "-r", ".")
        self.assertEqual(res.out, "rm 'a.txt'\nrm 'b.txt'\nrm 'd.txt'\n")
        self.assertEqual(self.ls_files(), [])
        self.assertTrue(os.path.isdir(self.path(".minigit")))

    def test_rm_is_all_or_nothing_and_rejects_untracked(self):
        self.write("untracked.txt", "u\n")
        res = self.fail_mg("rm", "a.txt", "nope")
        self.assertEqual(res.err, "error: pathspec 'nope' did not match any files\n")
        self.assertEqual(res.out, "")
        self.assertTrue(self.exists("a.txt"))
        self.assertIn("a.txt", self.ls_files())
        res = self.fail_mg("rm", "untracked.txt")
        self.assertEqual(res.err, "error: pathspec 'untracked.txt' did not match any files\n")
        self.assertTrue(self.exists("untracked.txt"))
        res = self.fail_mg("rm", "a.txt", "untracked.txt")
        self.assertTrue(self.exists("a.txt"))
        res = self.fail_mg("rm", code=2)
        self.assertIn("error: ", res.err)
        res = self.fail_mg("rm", "../outside")
        self.assertRegex(res.err, r"^error: '\.\./outside' is outside repository\n$")
        self.assertEqual(self.porcelain(), "?? untracked.txt\n")
        self.assertEqual(self.lock_files(), [])


# =========================================================================== commit

class CommitTests(Base):
    def setUp(self):
        super().setUp()
        self.init()

    def test_root_commit_objects_are_git_identical(self):
        files = {"a-b": b"1\n", "a.txt": b"2\n", "a/x": b"3\n", "a0": b"4\n", "a/y/z": b"5\n"}
        for rel, data in files.items():
            self.write(rel, data)
        self.mg("add", ".")
        res = self.mg("commit", "-m", "first")
        blobs = {rel: git_hash("blob", data) for rel, data in files.items()}
        tree_y = git_tree([("100644", "z", blobs["a/y/z"])])
        tree_a = git_tree([("100644", "x", blobs["a/x"]), ("40000", "y", git_hash("tree", tree_y))])
        tree_root = git_tree([("100644", "a-b", blobs["a-b"]), ("100644", "a.txt", blobs["a.txt"]),
                              ("40000", "a", git_hash("tree", tree_a)), ("100644", "a0", blobs["a0"])])
        root_sha = git_hash("tree", tree_root)
        commit_bytes = git_commit(root_sha, [], "first")
        commit_sha = git_hash("commit", commit_bytes)
        self.assertEqual(res.out.splitlines()[0], f"[main (root-commit) {commit_sha[:7]}] first")
        self.assertEqual(self.head(), commit_sha)
        self.assertEqual(self.gitfile("refs/heads/main"), commit_sha + "\n")
        self.assertEqual(self.gitfile("HEAD"), "ref: refs/heads/main\n")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD").stdout, commit_bytes)
        self.assertEqual(self.mg("cat-file", "-t", commit_sha).out, "commit\n")
        self.assertEqual(self.mg("cat-file", "-s", commit_sha).out, f"{len(commit_bytes)}\n")
        self.assertEqual(zlib.decompress(read(self.object_path(commit_sha))),
                         b"commit %d\0" % len(commit_bytes) + commit_bytes)
        self.assertEqual(zlib.decompress(read(self.object_path(root_sha))), b"tree %d\0" % len(tree_root) + tree_root)
        # the tree listing is in git order: "a-b" < "a.txt" < "a/" < "a0"
        self.assertEqual([(r[0], r[1], r[2], r[3]) for r in self.tree_listing("HEAD:")],
                         [("100644", "blob", blobs["a-b"], "a-b"), ("100644", "blob", blobs["a.txt"], "a.txt"),
                          ("040000", "tree", git_hash("tree", tree_a), "a"), ("100644", "blob", blobs["a0"], "a0")])
        self.assertEqual(self.mg("cat-file", "-t", "HEAD:a").out, "tree\n")
        self.assertEqual(self.mg("cat-file", "-t", "HEAD:a/y").out, "tree\n")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:a/y/z").stdout, b"5\n")
        res = self.fail_mg("cat-file", "-p", "HEAD:a/x/nothing")
        self.assertIn("does not exist in 'HEAD'", res.err)
        # exactly 5 blobs + 3 trees + 1 commit on disk, each at objects/xx/<38 hex>
        objs = self.object_files()
        self.assertEqual(len(objs), 9, objs)
        self.assertEqual(set(objs), set(blobs.values()) | {git_hash("tree", tree_y), git_hash("tree", tree_a), root_sha, commit_sha})
        self.assertEqual(self.mg("fsck").rc, 0)
        self.assertEqual(self.porcelain(), "")

    def test_second_commit_links_parent_and_summary_format(self):
        first = self.commit_file("a.txt", "a\n", "first")
        self.write("a.txt", "a2\n")
        self.mg("add", "a.txt")
        res = self.mg("commit", "-m", "second")
        second = self.head()
        self.assertEqual(res.out.splitlines(), [f"[main {second[:7]}] second", " 1 file changed, 1 insertion(+), 1 deletion(-)"])
        self.assertNotEqual(first, second)
        tree = git_hash("tree", git_tree([("100644", "a.txt", git_hash("blob", b"a2\n"))]))
        expected = git_commit(tree, [first], "second")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD").stdout, expected)
        self.assertEqual(second, git_hash("commit", expected))
        self.assertEqual(self.mg("rev-parse", "HEAD~1").out.strip(), first)
        self.assertEqual(self.mg("rev-parse", "HEAD^").out.strip(), first)
        self.assertEqual(self.mg("rev-parse", "main").out.strip(), second)
        self.assertEqual(self.mg("log", "--oneline").out, f"{second[:7]} (HEAD -> main) second\n{first[:7]} first\n")
        self.assertEqual(self.gitfile("refs/heads/main"), second + "\n")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD~1:a.txt").stdout, b"a\n")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:a.txt").stdout, b"a2\n")

    def test_commit_message_handling(self):
        self.commit_file("a.txt", "a\n", "base")
        cases = [
            (["-m", "subject", "-m", "body line 1\nbody line 2"], "subject\n\nbody line 1\nbody line 2\n", "subject"),
            (["-m", "trailing   \n\n\n"], "trailing\n", "trailing"),
            (["-m", "# looks like a comment but -m keeps it"], "# looks like a comment but -m keeps it\n",
             "# looks like a comment but -m keeps it"),
            (["-m", "ünïcode 🚀 message"], "ünïcode 🚀 message\n", "ünïcode 🚀 message"),
            (["-m", "a", "-m", ""], "a\n", "a"),
            (["-m", "multi\nline\nsubject only first"], "multi\nline\nsubject only first\n", "multi"),
        ]
        for args, expected_msg, subject in cases:
            with self.subTest(args=args):
                res = self.mg("commit", "--allow-empty", *args)
                sha = self.head()
                self.assertEqual(res.out, f"[main {sha[:7]}] {subject}\n")
                text = self.mg("cat-file", "-p", "HEAD").stdout
                head, _, msg = text.partition(b"\n\n")
                self.assertEqual(msg.decode("utf-8"), expected_msg)
                self.assertIn(b"\ncommitter " + f"{AUTHOR_NAME} <{AUTHOR_EMAIL}> {AUTHOR_DATE}".encode(), head)
                self.assertIn(f"\n    {subject}\n", self.mg("log", "-n", "1").out)

    def test_commit_requires_a_message_and_rejects_an_empty_one(self):
        self.write("a.txt", "a\n")
        self.mg("add", "a.txt")
        res = self.fail_mg("commit", code=2)
        self.assertEqual(res.err.splitlines()[-1], "error: commit message required: use -m")
        self.assertFalse(self.exists(".minigit/refs/heads/main"))
        for args in (["-m", ""], ["-m", "   "], ["-m", "\n\n"], ["-m", "", "-m", ""], ["-m", " \t \n "]):
            with self.subTest(args=args):
                res = self.fail_mg("commit", *args)
                self.assertEqual(res.err, "error: aborting commit due to empty commit message\n")
                self.assertEqual(res.out, "")
        self.assertFalse(self.exists(".minigit/refs/heads/main"), "no commit may be created")
        self.assertIn("No commits yet", self.mg("status").out)
        self.assertEqual(self.porcelain(), "A  a.txt\n", "the index must be untouched")
        self.assertEqual(self.lock_files(), [])
        self.mg("commit", "-m", "ok")
        self.assertRegex(self.gitfile("refs/heads/main"), r"^[0-9a-f]{40}\n$")

    def test_nothing_to_commit_and_allow_empty(self):
        res = self.fail_mg("commit", "-m", "nothing staged on an unborn branch")
        self.assertEqual(res.err, "error: nothing to commit, working tree clean\n")
        self.assertFalse(self.exists(".minigit/refs/heads/main"))
        res = self.mg("commit", "--allow-empty", "-m", "empty root")
        root = self.head()
        self.assertEqual(res.out, f"[main (root-commit) {root[:7]}] empty root\n")
        self.assertEqual(self.commit_tree(), EMPTY_TREE)
        self.assertEqual(self.mg("cat-file", "-t", EMPTY_TREE).out, "tree\n")
        self.assertEqual(self.mg("cat-file", "-s", EMPTY_TREE).out, "0\n")
        self.assertEqual(self.mg("cat-file", "-p", EMPTY_TREE).stdout, b"")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD").stdout, git_commit(EMPTY_TREE, [], "empty root"))
        self.commit_file("a.txt", "a\n", "a")
        base = self.head()
        self.write("untracked.txt", "u\n")
        res = self.fail_mg("commit", "-m", "clean index")
        # git's wording when only untracked files are present
        self.assertEqual(res.err, "error: nothing added to commit but untracked files present (use \"minigit add\" to track)\n")
        self.assertEqual(self.head(), base)
        self.write("a.txt", "dirty but unstaged\n")
        res = self.fail_mg("commit", "-m", "unstaged change is not a commit")
        # git's wording when a tracked file is modified but not staged
        self.assertEqual(res.err, 'error: no changes added to commit (use "minigit add" and/or "minigit commit -a")\n')
        self.assertEqual(self.head(), base)
        res = self.mg("commit", "--allow-empty", "-m", "same tree again")
        new = self.head()
        self.assertEqual(res.out, f"[main {new[:7]}] same tree again\n")
        self.assertEqual(self.commit_tree(new), self.commit_tree(base), "an empty commit reuses the parent tree")
        self.assertIn(f"parent {base}\n", self.mg("cat-file", "-p", "HEAD").out)
        self.assertEqual(self.porcelain(), " M a.txt\n?? untracked.txt\n")

    def test_commit_all_flag_and_snapshot_semantics(self):
        for rel in ("x.txt", "y.txt", "z.txt"):
            self.write(rel, rel + "\n")
        self.mg("add", ".")
        self.mg("commit", "-m", "base")
        self.write("x.txt", "x2\n")
        os.remove(self.path("y.txt"))
        self.write("untracked.txt", "u\n")
        res = self.mg("commit", "-a", "-m", "all")
        self.assertRegex(res.out, rf"^\[main {HEX7}\] all\n( \d+ files? changed.*\n)?$")
        self.assertEqual(self.porcelain(), "?? untracked.txt\n", "-a never stages untracked files")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:x.txt").stdout, b"x2\n")
        self.assertEqual(sorted(r[3] for r in self.tree_listing("HEAD:")), ["x.txt", "z.txt"])
        self.assertEqual(self.ls_files(), ["x.txt", "z.txt"])
        # a commit records the index, not the worktree
        self.write("z.txt", "staged\n")
        self.mg("add", "z.txt")
        self.write("z.txt", "changed after add\n")
        self.mg("commit", "-m", "snapshot")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:z.txt").stdout, b"staged\n")
        self.assertEqual(self.porcelain(), "?? untracked.txt\n M z.txt\n")
        self.assertEqual(self.read("z.txt"), b"changed after add\n")
        # -a with nothing modified is "nothing to commit"
        self.write("z.txt", "staged\n")
        res = self.fail_mg("commit", "-a", "-m", "no-op")
        # git's wording when an untracked file is still present
        self.assertEqual(res.err, 'error: nothing added to commit but untracked files present (use "minigit add" to track)\n')

    def test_identity_sources_and_missing_identity(self):
        self.write("a.txt", "a\n")
        self.mg("add", "a.txt")
        noid = self.make_env(identity=False)
        noid["MINIGIT_AUTHOR_DATE"] = AUTHOR_DATE
        res = self.fail_mg("commit", "-m", "x", env=noid)
        self.assertEqual(res.err, 'error: please set your identity: minigit config --global user.name "You"; '
                                  "minigit config --global user.email you@example.com\n")
        self.assertFalse(self.exists(".minigit/refs/heads/main"), "a failed commit must not create the branch")
        self.assertEqual(self.lock_files(), [])
        self.assertEqual(self.porcelain(), "A  a.txt\n")
        # only a name is not enough either
        half = dict(noid, MINIGIT_AUTHOR_NAME="Half")
        self.assertIn("please set your identity", self.fail_mg("commit", "-m", "x", env=half).err)
        # global config
        self.mg("config", "--global", "user.name", "Global User", env=noid)
        self.mg("config", "--global", "user.email", "global@example.com", env=noid)
        self.assertTrue(os.path.isfile(noid["MINIGIT_GLOBAL_CONFIG"]))
        self.mg("commit", "-m", "from global", env=noid)
        self.assertIn(f"author Global User <global@example.com> {AUTHOR_DATE}\n", self.mg("cat-file", "-p", "HEAD").out)
        self.assertIn("Author: Global User <global@example.com>", self.mg("log", "-n", "1").out)
        # local config beats global
        self.mg("config", "user.name", "Local User", env=noid)
        self.mg("config", "user.email", "local@example.com", env=noid)
        self.mg("commit", "--allow-empty", "-m", "from local", env=noid)
        self.assertIn("author Local User <local@example.com> ", self.mg("cat-file", "-p", "HEAD").out)
        # environment beats both; the committer always equals the author
        self.mg("commit", "--allow-empty", "-m", "from env")
        text = self.mg("cat-file", "-p", "HEAD").out
        self.assertIn(f"author {AUTHOR_NAME} <{AUTHOR_EMAIL}> {AUTHOR_DATE}\n", text)
        self.assertIn(f"committer {AUTHOR_NAME} <{AUTHOR_EMAIL}> {AUTHOR_DATE}\n", text)
        # angle brackets and newlines cannot break the header
        weird = dict(self.env, MINIGIT_AUTHOR_NAME="Bad <Guy>\nInjected", MINIGIT_AUTHOR_EMAIL="<x@y>")
        self.mg("commit", "--allow-empty", "-m", "weird", env=weird)
        text = self.mg("cat-file", "-p", "HEAD").out
        header = text.split("\n\n", 1)[0]
        author_lines = [l for l in header.split("\n") if l.startswith("author ")]
        self.assertEqual(len(author_lines), 1, header)
        m = re.fullmatch(r"author (.*) <x@y> 1700000000 \+0000", author_lines[0])
        self.assertIsNotNone(m, author_lines[0])
        self.assertNotIn("<", m.group(1))
        self.assertNotIn(">", m.group(1))
        self.assertTrue(m.group(1).startswith("Bad Guy"), m.group(1))
        self.assertEqual(self.mg("fsck").rc, 0)
        self.assertRegex(self.mg("log", "-n", "1").out, r"\nAuthor: Bad Guy.* <x@y>\n")

    def test_author_date_environment(self):
        self.write("a.txt", "a\n")
        self.mg("add", "a.txt")
        env = dict(self.env, MINIGIT_AUTHOR_DATE="1700000000 +0530")
        self.mg("commit", "-m", "ist", env=env)
        text = self.mg("cat-file", "-p", "HEAD").out
        self.assertIn(f"\nauthor {AUTHOR_NAME} <{AUTHOR_EMAIL}> 1700000000 +0530\n", text)
        self.assertIn("\nDate:   Wed Nov 15 03:43:20 2023 +0530\n", self.mg("log", "-n", "1").out)
        env = dict(self.env, MINIGIT_AUTHOR_DATE="1700000000 -0800")
        self.mg("commit", "--allow-empty", "-m", "pst", env=env)
        self.assertIn("1700000000 -0800\n", self.mg("cat-file", "-p", "HEAD").out)
        self.assertIn("\nDate:   Tue Nov 14 14:13:20 2023 -0800\n", self.mg("log", "-n", "1").out)
        head = self.head()
        for bad in ("garbage", "1700000000 +53", "1700000000 UTC", "12:00 +0000"):
            with self.subTest(date=bad):
                res = self.fail_mg("commit", "--allow-empty", "-m", "bad", env=dict(self.env, MINIGIT_AUTHOR_DATE=bad))
                self.assertTrue(res.err.startswith("error: "), str(res))
                self.assertNotIn("internal error", res.err)
                self.assertEqual(self.head(), head)
        # without the variable the commit uses the current time
        env = self.make_env()
        env.pop("MINIGIT_AUTHOR_DATE")
        before = int(time.time()) - 5
        self.mg("commit", "--allow-empty", "-m", "now", env=env)
        m = re.search(r"\nauthor .* (\d+) ([+-]\d{4})\n", self.mg("cat-file", "-p", "HEAD").out)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), before)
        self.assertLessEqual(int(m.group(1)), int(time.time()) + 5)

    def test_commit_on_detached_head(self):
        c1 = self.commit_file("a.txt", "v1\n", "c1")
        c2 = self.commit_file("a.txt", "v2\n", "c2")
        self.mg("checkout", c1)
        self.assertEqual(self.gitfile("HEAD"), c1 + "\n")
        self.write("d.txt", "d\n")
        self.mg("add", "d.txt")
        res = self.mg("commit", "-m", "detached work")
        d = self.head()
        self.assertEqual(res.out.splitlines()[0], f"[detached HEAD {d[:7]}] detached work")
        self.assertEqual(self.gitfile("HEAD"), d + "\n", "a detached commit moves HEAD itself")
        self.assertEqual(self.gitfile("refs/heads/main"), c2 + "\n", "the branch must not move")
        self.assertIn(f"parent {c1}\n", self.mg("cat-file", "-p", "HEAD").out)
        self.assertIn(f"HEAD detached at {d[:7]}", self.mg("status").out)
        self.assertRegex(self.mg("log", "--oneline", "-n", "1").out, rf"^{d[:7]} \(HEAD\) detached work\n$")

    def test_commit_fails_cleanly_when_the_branch_is_locked(self):
        base = self.commit_file("a.txt", "a\n", "base")
        self.write("a.txt", "a2\n")
        self.mg("add", "a.txt")
        lock = self.path(".minigit/refs/heads/main.lock")
        write(lock, b"")
        res = self.fail_mg("commit", "-m", "blocked")
        self.assertIn("error: unable to lock", res.err)
        self.assertIn("main.lock exists", res.err)
        self.assertEqual(res.out, "")
        self.assertEqual(self.gitfile("refs/heads/main"), base + "\n")
        self.assertEqual(self.head(), base)
        self.assertTrue(os.path.exists(lock))
        self.assertEqual(self.lock_files(), [lock], "the failing commit must leave no index.lock behind")
        self.assertEqual(self.porcelain(), "M  a.txt\n", "the staged change survives")
        os.remove(lock)
        self.mg("commit", "-m", "unblocked")
        self.assertEqual(self.porcelain(), "")
        self.assertEqual(self.mg("fsck").rc, 0)
        # the index lock blocks commit as well
        write(self.path(".minigit/index.lock"), b"")
        res = self.fail_mg("commit", "--allow-empty", "-m", "x")
        self.assertIn("index.lock exists", res.err)
        os.remove(self.path(".minigit/index.lock"))

    def test_corrupt_index_is_reported_not_crashed(self):
        self.commit_file("a.txt", "a\n", "base")
        write(self.path(".minigit/index"), b"MIDX garbage that is not an index")
        msg = "error: index file is corrupt; run 'minigit reset' to rebuild it from HEAD\n"
        for args in (["add", "a.txt"], ["commit", "-m", "x"], ["ls-files"], ["status"], ["rm", "a.txt"]):
            with self.subTest(args=args):
                res = self.fail_mg(*args)
                self.assertEqual(res.err, msg)
        self.assertTrue(self.exists("a.txt"), "rm on a corrupt index must not delete the file")
        self.assertEqual(self.lock_files(), [])
        self.mg("reset")
        self.assertEqual(self.ls_files(), ["a.txt"])
        self.assertEqual(self.porcelain(), "")

    def test_concurrent_commits_never_corrupt_the_repository(self):
        self.commit_file("a.txt", "a\n", "base")
        n = 6
        procs = [_popen(["commit", "--allow-empty", "-m", f"c{i}"], self.wt, self.env) for i in range(n)]
        results = []
        for i, p in enumerate(procs):
            out, err = p.communicate(timeout=TIMEOUT)
            results.append(Result([f"c{i}"], p.returncode, out, err))
        ok = [r for r in results if r.rc == 0]
        failed = [r for r in results if r.rc != 0]
        self.assertGreaterEqual(len(ok), 1, "\n".join(map(str, results)))
        for r in ok:
            self.assertRegex(r.out, rf"^\[main {HEX7}\] c\d\n$")
        for r in failed:
            self.assertEqual(r.rc, 1, str(r))
            self.assertIn("unable to lock", r.err, str(r))
            self.assertEqual(r.out, "")
        lines = self.mg("log", "--oneline").out.splitlines()
        self.assertEqual(len(lines), 1 + len(ok), "every successful commit is on the chain exactly once")
        subjects = sorted(line.split(" ", 1)[1].split(") ")[-1] for line in lines)
        self.assertEqual(subjects, sorted(["base"] + [r.out.split("] ", 1)[1].strip() for r in ok]))
        self.assertEqual(self.lock_files(), [])
        self.assertEqual(self.mg("fsck").out, "")
        self.assertEqual(self.porcelain(), "")
        self.assertRegex(self.gitfile("refs/heads/main"), r"^[0-9a-f]{40}\n$")


# =========================================================================== object store

class ObjectStoreTests(Base):
    def setUp(self):
        super().setUp()
        self.init()

    def test_identical_content_is_stored_once_and_writes_are_idempotent(self):
        self.write("one.txt", "same\n")
        self.write("two/dup.txt", "same\n")
        self.write("three.txt", "same\n")
        self.mg("add", ".")
        sha = git_hash("blob", b"same\n")
        self.assertEqual({v[1] for v in self.ls_stage().values()}, {sha})
        self.assertEqual(self.object_files(), [sha])
        raw_before = read(self.object_path(sha))
        self.mg("commit", "-m", "dups")
        self.assertEqual(len(self.object_files()), 1 + 2 + 1, "1 blob, 2 trees, 1 commit")
        self.write("one.txt", "same\n")  # rewrite, re-add: no new object
        self.mg("add", ".")
        self.assertEqual(len(self.object_files()), 4)
        self.assertEqual(read(self.object_path(sha)), raw_before, "an existing object is never rewritten")
        self.assertEqual(self.mg("hash-object", "one.txt").out, sha + "\n")
        self.write("new.txt", "fresh\n")
        fresh = git_hash("blob", b"fresh\n")
        self.assertEqual(self.mg("hash-object", "new.txt").out, fresh + "\n")
        self.assertFalse(os.path.exists(self.object_path(fresh)), "hash-object without -w writes nothing")
        self.assertEqual(self.mg("hash-object", "-w", "new.txt").out, fresh + "\n")
        self.assertTrue(os.path.exists(self.object_path(fresh)))
        self.assertEqual(self.mg("cat-file", "-p", fresh).stdout, b"fresh\n")
        self.assertEqual(self.mg("fsck").rc, 0, "a dangling blob is not a problem")
        for d in os.listdir(self.path(".minigit/objects")):
            sub = self.path(".minigit/objects/" + d)
            if os.path.isdir(sub):
                self.assertEqual(len(d), 2, d)
                for n in os.listdir(sub):
                    self.assertEqual(len(n), 38, n)
                    self.assertNotIn("tmp", n, "no temporary files left behind")

    def test_corrupt_truncated_and_missing_objects_are_detected(self):
        self.commit_file("h.txt", "hello\n", "h")
        path = self.object_path(HELLO_BLOB)
        good = read(path)
        # content changed, hash no longer matches
        write(path, zlib.compress(b"blob 6\0hellX\n"))
        res = self.fail_mg("cat-file", "-p", "HEAD:h.txt")
        self.assertIn("error: ", res.err)
        self.assertIn(HELLO_BLOB, res.err)
        self.assertIn("corrupt", res.err)
        self.assertEqual(res.out, "")
        res = self.fail_mg("fsck")
        self.assertIn(HELLO_BLOB, res.out)
        res = self.fail_mg("show", "HEAD:h.txt")
        self.assertIn("corrupt", res.err)
        # header says a different length
        write(path, zlib.compress(b"blob 5\0hello\n"))
        res = self.fail_mg("cat-file", "-p", HELLO_BLOB)
        self.assertIn("corrupt", res.err)
        # truncated zlib stream
        write(path, good[: len(good) // 2])
        res = self.fail_mg("cat-file", "-p", HELLO_BLOB)
        self.assertIn("corrupt", res.err)
        self.assertNotIn("internal error", res.err)
        self.assertEqual(self.fail_mg("fsck").rc, 1)
        # plain garbage that is not zlib at all
        write(path, b"this is not zlib data")
        res = self.fail_mg("cat-file", "-p", HELLO_BLOB)
        self.assertNotIn("internal error", res.err)
        self.assertNotIn("Traceback", res.err)
        # missing
        os.remove(path)
        res = self.fail_mg("cat-file", "-p", "HEAD:h.txt")
        self.assertEqual(res.err, f"error: object {HELLO_BLOB} not found\n")
        res = self.fail_mg("fsck")
        self.assertIn(f"missing object {HELLO_BLOB}", res.out)
        self.assertEqual(self.mg("rev-parse", "HEAD").rc, 0, "refs still resolve")
        # re-adding the identical content restores the object byte for byte
        self.mg("add", "h.txt")
        self.assertEqual(read(path), good)
        self.assertEqual(self.mg("fsck").out, "")
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:h.txt").stdout, b"hello\n")
        # the same for tree and commit objects
        self.commit_file("a.txt", "a\n", "base")
        commit = self.head()
        tree = self.commit_tree()
        # tree with a bad mode
        bad_tree = b"100644 a.txt\0" + bytes.fromhex(git_hash("blob", b"a\n"))
        bad_tree = bad_tree.replace(b"100644", b"123456")
        write(self.object_path(tree), zlib.compress(b"tree %d\0" % len(bad_tree) + bad_tree))
        res = self.fail_mg("fsck")
        self.assertIn(tree, res.out)
        res = self.fail_mg("cat-file", "-p", "HEAD:a.txt")
        self.assertNotIn("internal error", res.err)
        self.assertNotIn("Traceback", res.err)
        res = self.fail_mg("status")
        self.assertNotIn("internal error", res.err)
        # a commit object whose stored bytes are not a commit
        write(self.object_path(commit), zlib.compress(b"commit 7\0garbage"))
        for args in (["log"], ["cat-file", "-p", "HEAD"], ["commit", "-m", "x"], ["status"]):
            with self.subTest(args=args):
                res = self.fail_mg(*args)
                self.assertTrue(res.err.startswith("error: "), str(res))
                self.assertNotIn("Traceback", res.err)
        self.assertEqual(self.gitfile("refs/heads/main"), commit + "\n", "nothing may be rewritten")


if __name__ == "__main__":
    unittest.main()

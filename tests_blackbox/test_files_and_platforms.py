"""Black-box QA suite: files and platforms.

Covers binary files, large files, unicode/spaces in names, deep directories,
CRLF vs LF, deleted/renamed files and Windows paths. Every test drives the
real CLI (``python -m minigit ...``) as a subprocess inside a temporary
directory. Where a test needs a commit that the CLI cannot produce (for
example a tree entry that is legal on Linux but not on Windows, or an
executable bit on Windows), it writes git-identical loose objects straight
into ``.minigit/objects``; SPEC section 2 defines that format as git's.

Run from E:\\ideas\\minigit with:
    python -m unittest tests_blackbox.test_files_and_platforms
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IS_WIN = os.name == "nt"
DEFAULT_TIMEOUT = 120


# --------------------------------------------------------------------------- helpers

class Res:
    def __init__(self, args, code, out, err):
        self.args, self.code, self.out, self.err = args, code, out, err

    @property
    def text(self) -> str:
        return (self.out + self.err).decode("utf-8", "replace")

    def __str__(self) -> str:
        return (f"minigit {' '.join(self.args)} -> exit {self.code}\n"
                f"stdout: {self.out[:2000]!r}\nstderr: {self.err[:2000]!r}")


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def write(path: str, data) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def write_object(gitdir: str, obj_type: str, data: bytes) -> str:
    raw = b"%s %d\0" % (obj_type.encode(), len(data)) + data
    sha = hashlib.sha1(raw).hexdigest()
    d = os.path.join(gitdir, "objects", sha[:2])
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, sha[2:])
    if not os.path.exists(p):
        with open(p, "wb") as fh:
            fh.write(zlib.compress(raw))
    return sha


def write_tree(gitdir: str, entries) -> str:
    """entries: iterable of (mode, name, sha) with name as str or bytes. Git order."""
    norm = [(m, n.encode("utf-8") if isinstance(n, str) else n, s) for m, n, s in entries]
    norm.sort(key=lambda e: e[1] + (b"/" if e[0] == "40000" else b""))
    data = b"".join(m.encode() + b" " + n + b"\0" + bytes.fromhex(s) for m, n, s in norm)
    return write_object(gitdir, "tree", data)


def write_commit(gitdir: str, tree: str, parents=(), message: str = "crafted") -> str:
    sig = b"Crafter <crafter@example.com> 1700000100 +0000"
    lines = [b"tree " + tree.encode()] + [b"parent " + p.encode() for p in parents]
    data = b"\n".join(lines + [b"author " + sig, b"committer " + sig, b"", message.encode()]) + b"\n"
    return write_object(gitdir, "commit", data)


def nested_tree(gitdir: str, path: str, blob: str, mode: str = "100644") -> tuple[str, str, str]:
    """Build the trees for a single file at ``path``; return (top_name, top_sha, top_mode)."""
    parts = path.split("/")
    sha, m = blob, mode
    for name in reversed(parts[1:]):
        sha = write_tree(gitdir, [(m, name, sha)])
        m = "40000"
    return parts[0], sha, m


def fs_is_case_insensitive(directory: str) -> bool:
    probe = os.path.join(directory, "CaseProbe.tmp")
    write(probe, b"")
    try:
        return os.path.exists(os.path.join(directory, "caseprobe.TMP"))
    finally:
        os.remove(probe)


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
    for k in ("MINIGIT_DEBUG", "MINIGIT_USER", "MINIGIT_TOKEN"):
        env.pop(k, None)
    prev = env.get("PYTHONPATH")
    env["PYTHONPATH"] = ROOT if not prev else ROOT + os.pathsep + prev
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class Base(unittest.TestCase):
    """Temp dir (with a space in its name, as on many Windows profiles) + CLI runner."""

    tmp_prefix = "mg qa "

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix=self.tmp_prefix, ignore_cleanup_errors=True)
        self.addCleanup(self._cleanup_tmp)
        self.tmp = self._td.name
        self.env = make_env(self.tmp)

    def _cleanup_tmp(self):
        # make read-only leftovers deletable, then remove
        for dirpath, dirnames, filenames in os.walk(self.tmp):
            for name in filenames:
                try:
                    os.chmod(os.path.join(dirpath, name), stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
        self._td.cleanup()

    def mg(self, *args, cwd=None, check=True, timeout=DEFAULT_TIMEOUT, env=None) -> Res:
        args = [str(a) for a in args]
        try:
            p = subprocess.run([sys.executable, "-m", "minigit", *args], cwd=cwd or self.repo,
                               env=env or self.env, capture_output=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            self.fail(f"minigit {' '.join(args)} did not finish within {timeout} s")
        r = Res(args, p.returncode, p.stdout, p.stderr)
        if check and r.code != 0:
            self.fail(f"command failed unexpectedly:\n{r}")
        return r

    def init_repo(self, name="repo") -> str:
        self.repo = os.path.join(self.tmp, name)
        os.makedirs(self.repo, exist_ok=True)
        self.mg("init", cwd=self.repo)
        return self.repo

    def p(self, rel: str) -> str:
        return os.path.join(self.repo, *rel.split("/"))

    @property
    def gitdir(self) -> str:
        return os.path.join(self.repo, ".minigit")

    def porcelain(self) -> list[str]:
        out = self.mg("status", "--porcelain").out.decode("utf-8")
        return [ln for ln in out.split("\n") if ln]

    def ls_stage(self) -> dict[str, tuple[str, str, str]]:
        """path -> (mode, sha, stage) for every line of ``ls-files -s`` (last stage wins)."""
        res = {}
        for ln in self.mg("ls-files", "-s").out.decode("utf-8").split("\n"):
            if not ln:
                continue
            meta, path = ln.split("\t", 1)
            mode, sha, stage = meta.split()
            res[(path, stage)] = (mode, sha, stage)
        return res

    def ls_files(self) -> list[str]:
        return [ln for ln in self.mg("ls-files").out.decode("utf-8").split("\n") if ln]

    def head_sha(self) -> str:
        return self.mg("rev-parse", "HEAD").out.decode().strip()

    def head_raw(self) -> str:
        return read(os.path.join(self.gitdir, "HEAD")).decode().strip()

    def commit_all(self, msg="c"):
        self.mg("add", ".")
        return self.mg("commit", "-m", msg)

    def base_repo(self):
        """Repo with one commit holding base.txt = b'base\\n'."""
        self.init_repo()
        write(self.p("base.txt"), b"base\n")
        self.commit_all("base")
        return self.head_sha()


# --------------------------------------------------------------------------- binary files

class BinaryFiles(Base):

    def test_all_byte_values_roundtrip_and_hash_like_git(self):
        self.init_repo()
        data = bytes(range(256)) * 64 + b"\r\n\r\r\n\x00\x1a"   # every byte, CR/LF mixes, NUL, Ctrl-Z
        write(self.p("bin/all bytes.dat"), data)
        write(self.p("empty.txt"), b"")
        self.commit_all("binary")
        st = self.ls_stage()
        self.assertEqual(st[("bin/all bytes.dat", "0")][1], blob_sha(data))
        self.assertEqual(st[("empty.txt", "0")][1], "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
        # stdout must be raw bytes (no text-mode \n -> \r\n on Windows)
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:bin/all bytes.dat").out, data)
        self.assertEqual(self.mg("show", "HEAD:empty.txt").out, b"")
        os.remove(self.p("bin/all bytes.dat"))
        os.remove(self.p("empty.txt"))
        self.mg("restore", "bin", "empty.txt")
        self.assertEqual(read(self.p("bin/all bytes.dat")), data)
        self.assertEqual(read(self.p("empty.txt")), b"")
        self.assertEqual(self.porcelain(), [])

    def test_lf_blob_printed_without_newline_translation(self):
        self.init_repo()
        data = "line one\nline two ünï\n".encode("utf-8")
        write(self.p("lf.txt"), data)
        self.commit_all()
        self.assertEqual(self.mg("show", "HEAD:lf.txt").out, data)
        self.assertEqual(self.mg("cat-file", "-p", "HEAD:lf.txt").out, data)

    def test_binary_detection_8000_byte_boundary_and_utf16(self):
        self.init_repo()
        write(self.p("n7999.dat"), b"a" * 7999 + b"\0tail\n")    # NUL is byte #8000 -> binary
        write(self.p("n8000.dat"), b"a" * 8000 + b"\0tail\n")    # NUL is byte #8001 -> text
        write(self.p("u16.txt"), "hello\n".encode("utf-16"))
        self.commit_all()
        for name in ("n7999.dat", "n8000.dat"):
            with open(self.p(name), "ab") as fh:
                fh.write(b"more\n")
        write(self.p("u16.txt"), "hellO\n".encode("utf-16"))
        out = self.mg("diff").out
        self.assertIn(b"Binary files a/n7999.dat and b/n7999.dat differ", out)
        self.assertNotIn(b"Binary files a/n8000.dat", out)
        self.assertIn(b"+more\n", out)
        self.assertIn(b"Binary files a/u16.txt and b/u16.txt differ", out)

    def test_binary_merge_conflict_keeps_ours_and_records_stages(self):
        self.init_repo()
        base, ours, theirs = b"B\0base", b"B\0ours\xff", b"B\0theirs\xfe"
        write(self.p("img.bin"), base)
        self.commit_all("base")
        self.mg("switch", "-c", "feat")
        write(self.p("img.bin"), theirs)
        self.mg("commit", "-am", "theirs")
        self.mg("switch", "main")
        write(self.p("img.bin"), ours)
        self.mg("commit", "-am", "ours")
        r = self.mg("merge", "feat", check=False)
        self.assertEqual(r.code, 1, str(r))
        self.assertIn("CONFLICT (binary): Merge conflict in img.bin", r.text)
        self.assertEqual(read(self.p("img.bin")), ours)
        st = self.ls_stage()
        self.assertEqual(st[("img.bin", "1")][1], blob_sha(base))
        self.assertEqual(st[("img.bin", "2")][1], blob_sha(ours))
        self.assertEqual(st[("img.bin", "3")][1], blob_sha(theirs))
        self.assertIn("UU img.bin", self.porcelain())


# --------------------------------------------------------------------------- large files

class LargeFiles(Base):

    def test_16mib_random_binary_roundtrip(self):
        self.init_repo()
        data = os.urandom(16 * 1024 * 1024)
        write(self.p("big.bin"), data)
        self.commit_all("big")
        self.assertEqual(self.ls_stage()[("big.bin", "0")][1], blob_sha(data))
        self.assertEqual(self.mg("cat-file", "-s", "HEAD:big.bin").out.strip(), str(len(data)).encode())
        os.remove(self.p("big.bin"))
        self.mg("restore", "big.bin")
        self.assertEqual(read(self.p("big.bin")), data)
        self.assertEqual(self.porcelain(), [])

    def test_file_over_64mib_is_refused_or_readable_back(self):
        """A file > 64 MiB must either be refused by add, or be fully readable after commit."""
        self.init_repo()
        size = 65 * 1024 * 1024
        data = (b"0123456789abcdef" * (size // 16))
        write(self.p("huge.bin"), data)
        r = self.mg("add", "huge.bin", check=False, timeout=300)
        if r.code != 0:
            self.assertNotIn("internal error", r.text)
            self.assertNotIn("huge.bin", self.ls_files())
            return
        self.mg("commit", "-m", "huge", timeout=300)
        os.remove(self.p("huge.bin"))
        r = self.mg("restore", "huge.bin", check=False, timeout=300)
        self.assertEqual(r.code, 0, f"committed file cannot be restored (data loss):\n{r}")
        self.assertEqual(read(self.p("huge.bin")), data)
        r = self.mg("fsck", check=False, timeout=300)
        self.assertEqual(r.code, 0, f"fsck reports the repository corrupt after a normal commit:\n{r}")

    def test_large_text_single_line_change_hunk(self):
        self.init_repo()
        lines = [b"line %d\n" % i for i in range(100000)]
        write(self.p("big.txt"), b"".join(lines))
        self.commit_all()
        lines[50000] = b"CHANGED\n"
        write(self.p("big.txt"), b"".join(lines))
        out = self.mg("diff", timeout=60).out
        self.assertIn(b"@@ -49998,7 +49998,7 @@\n", out)
        self.assertIn(b"-line 50000\n+CHANGED\n", out)

    def test_diff_of_repetitive_text_finishes_quickly(self):
        """10,000 identical lines + one inserted line (40 KB): git diffs this instantly."""
        self.init_repo()
        write(self.p("dup.txt"), b"x\n" * 10000)
        self.commit_all()
        write(self.p("dup.txt"), b"x\n" * 5000 + b"y\n" + b"x\n" * 5000)
        start = time.monotonic()
        out = self.mg("diff", "--", "dup.txt", timeout=20).out
        self.assertIn(b"+y\n", out)
        self.assertLess(time.monotonic() - start, 20)


# --------------------------------------------------------------------------- CRLF vs LF

class LineEndings(Base):

    def test_crlf_mixed_cr_and_no_final_newline_preserved_byte_for_byte(self):
        self.init_repo()
        files = {
            "crlf.txt": b"a\r\nb\r\n",
            "mixed.txt": b"lf\ncrlf\r\nlf\n\r\n",
            "cr-only.txt": b"old\rmac\r",
            "no-eol.txt": b"a\r\nb",
        }
        for name, data in files.items():
            write(self.p(name), data)
        self.commit_all("eol")
        st = self.ls_stage()
        for name, data in files.items():
            self.assertEqual(st[(name, "0")][1], blob_sha(data), name)
        self.mg("switch", "-c", "other")
        for name in files:
            os.remove(self.p(name))
        write(self.p("x.txt"), b"x\n")
        self.commit_all("other")
        self.assertFalse(os.path.exists(self.p("crlf.txt")))
        self.mg("switch", "main")
        for name, data in files.items():
            self.assertEqual(read(self.p(name)), data, name)
        self.assertEqual(self.porcelain(), [])
        # diff keeps the \r and flags the missing final newline
        write(self.p("no-eol.txt"), b"a\r\nB")
        out = self.mg("diff", "--", "no-eol.txt").out
        self.assertIn(b"-b\n\\ No newline at end of file\n+B\n\\ No newline at end of file\n", out)

    def test_line_ending_only_change_is_a_modification(self):
        self.init_repo()
        write(self.p("f.txt"), b"one\ntwo\n")
        self.commit_all()
        write(self.p("f.txt"), b"one\r\ntwo\r\n")
        self.assertEqual(self.porcelain(), [" M f.txt"])
        out = self.mg("diff").out
        self.assertIn(b"-one\n-two\n+one\r\n+two\r\n", out)
        self.mg("commit", "-am", "crlf")
        self.assertEqual(self.mg("show", "HEAD:f.txt").out, b"one\r\ntwo\r\n")

    def test_conflict_markers_use_crlf_for_crlf_files(self):
        self.init_repo()
        write(self.p("c.txt"), b"l1\r\nl2\r\nl3\r\n")
        self.commit_all("base")
        self.mg("switch", "-c", "feat")
        write(self.p("c.txt"), b"l1\r\nFEAT\r\nl3\r\n")
        self.mg("commit", "-am", "feat")
        self.mg("switch", "main")
        write(self.p("c.txt"), b"l1\r\nMAIN\r\nl3\r\n")
        self.mg("commit", "-am", "main")
        r = self.mg("merge", "feat", check=False)
        self.assertEqual(r.code, 1, str(r))
        content = read(self.p("c.txt"))
        self.assertEqual(content,
                         b"l1\r\n<<<<<<< HEAD\r\nMAIN\r\n=======\r\nFEAT\r\n>>>>>>> feat\r\nl3\r\n")

    def test_crlf_ignore_file_and_crlf_config(self):
        self.init_repo()
        write(self.p(".minigitignore"), b"*.log\r\nbuild/\r\n!keep.log\r\n")
        write(self.p("a.log"), b"l\n")
        write(self.p("keep.log"), b"k\n")
        write(self.p("build/o.txt"), b"o\n")
        write(self.p("t.txt"), b"t\n")
        self.assertEqual(self.porcelain(), ["?? .minigitignore", "?? keep.log", "?? t.txt"])
        write(self.env["MINIGIT_GLOBAL_CONFIG"],
              b"[user]\r\n\tname = Win User\r\n\temail = win@example.com\r\n")
        env = dict(self.env)
        env.pop("MINIGIT_AUTHOR_NAME")
        env.pop("MINIGIT_AUTHOR_EMAIL")
        self.assertEqual(self.mg("config", "user.name", env=env).out, b"Win User\n")
        self.mg("add", "t.txt", env=env)
        self.mg("commit", "-m", "x", env=env)
        raw = self.mg("cat-file", "-p", "HEAD", env=env).out
        self.assertIn(b"author Win User <win@example.com> 1700000000 +0000\n", raw)

    def test_utf8_bom_in_global_config_is_ignored(self):
        """Windows PowerShell 5.1 'Out-File -Encoding utf8' and old Notepad write a BOM; git skips it."""
        self.init_repo()
        write(self.env["MINIGIT_GLOBAL_CONFIG"],
              b"\xef\xbb\xbf[user]\n\tname = Bom User\n\temail = bom@example.com\n")
        env = dict(self.env)
        env.pop("MINIGIT_AUTHOR_NAME")
        env.pop("MINIGIT_AUTHOR_EMAIL")
        r = self.mg("config", "user.name", env=env, check=False)
        self.assertEqual((r.code, r.out), (0, b"Bom User\n"), str(r))
        write(self.p("t.txt"), b"t\n")
        self.mg("add", "t.txt", env=env)
        r = self.mg("commit", "-m", "x", env=env, check=False)
        self.assertEqual(r.code, 0, str(r))


# --------------------------------------------------------------------------- unicode / spaces / odd names

class Names(Base):

    def test_unicode_space_and_punctuation_names_full_cycle(self):
        self.init_repo()
        names = ["日本 dir/ファイル 名.txt", "dïr ü/fïlé.txt", " lead space.txt", "a#b.txt",
                 "100%.txt", "it's.txt", "semi;colon.txt", "tilde~1.txt", "at@{1}.txt",
                 "brace{a,b}.txt", "excl!.txt", "emoji \U0001F600.txt", "x.txt"]
        for i, n in enumerate(names):
            write(self.p(n), b"content %d\n" % i)
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), sorted(names, key=lambda s: s.encode("utf-8")))
        self.mg("commit", "-m", "names")
        for i, n in enumerate(names):
            self.assertEqual(self.mg("show", f"HEAD:{n}").out, b"content %d\n" % i, n)
        write(self.p("日本 dir/ファイル 名.txt"), b"changed\n")
        self.assertEqual(self.porcelain(), [" M 日本 dir/ファイル 名.txt"])
        out = self.mg("diff").out
        self.assertIn("diff --git a/日本 dir/ファイル 名.txt b/日本 dir/ファイル 名.txt\n".encode(), out)
        self.mg("rm", "-f", "emoji \U0001F600.txt")
        self.assertFalse(os.path.exists(self.p("emoji \U0001F600.txt")))
        self.mg("checkout", "HEAD", "--", "emoji \U0001F600.txt")
        self.assertEqual(read(self.p("emoji \U0001F600.txt")), b"content 11\n")

    def test_tree_hash_matches_real_git(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git not on PATH")
        layout = {"a-b": b"x", "a.txt": b"y\r\n", "a/inner": b"z", "a0": b"",
                  "a b/ünï/日本/bin 名.dat": b"\0\xff", "deep/1/2/3/4/5/6/7/8/9/leaf": b"d\n",
                  "Zeta": b"e", "_u": b"f", "~t": b"g", "sp ace/ x ": b"h"}
        if IS_WIN:
            layout.pop("sp ace/ x ")   # trailing space is not representable on Windows
        self.init_repo()
        gdir = os.path.join(self.tmp, "gitrepo")
        for rel, data in layout.items():
            write(self.p(rel), data)
            write(os.path.join(gdir, *rel.split("/")), data)
        self.commit_all()
        ours = self.mg("rev-parse", "HEAD:").out.decode().strip()
        genv = dict(self.env, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        gopts = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.filemode=false"]
        subprocess.run([git, "init", "-q", gdir], check=True, env=genv, capture_output=True)
        subprocess.run([git, *gopts, "add", "."], cwd=gdir, check=True, env=genv, capture_output=True)
        theirs = subprocess.run([git, *gopts, "write-tree"], cwd=gdir, check=True, env=genv,
                                capture_output=True).stdout.decode().strip()
        self.assertEqual(ours, theirs)

    def test_bracket_name_is_added_literally_not_as_glob(self):
        self.init_repo()
        write(self.p("[x].txt"), b"lit\n")
        write(self.p("x.txt"), b"x\n")
        self.mg("add", "[x].txt")
        self.assertEqual(self.ls_files(), ["[x].txt"])

    def test_dash_prefixed_file_name(self):
        self.init_repo()
        write(self.p("-dash.txt"), b"d\n")
        self.mg("add", "--", "-dash.txt")
        self.assertEqual(self.ls_files(), ["-dash.txt"])
        self.mg("commit", "-m", "dash")
        self.mg("rm", "--", "-dash.txt")
        self.assertFalse(os.path.exists(self.p("-dash.txt")))
        self.assertEqual(self.porcelain(), ["D  -dash.txt"])

    def test_unicode_output_without_pythonioencoding(self):
        """Pipes on Windows default to cp1252; output must still be UTF-8 bytes (SPEC M7)."""
        self.init_repo()
        env = dict(self.env)
        for k in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO"):
            env.pop(k, None)
        write(self.p("名前 ü.txt"), b"x\n")
        self.mg("add", ".", env=env)
        self.assertEqual(self.mg("status", "-s", env=env).out, "A  名前 ü.txt\n".encode("utf-8"))
        self.assertIn("\tnew file:   名前 ü.txt\n".encode(), self.mg("status", env=env).out)
        r = self.mg("commit", "-m", "ünï 名", env=env)
        self.assertIn("ünï 名".encode(), r.out)
        self.assertIn("ünï 名".encode(), self.mg("log", "--oneline", env=env).out)
        r = self.mg("rm", "nope 名.txt", env=env, check=False)
        self.assertEqual(r.code, 1, str(r))
        self.assertIn("nope 名.txt".encode("utf-8"), r.err)
        self.assertNotIn(b"internal error", r.err)

    def test_repo_root_with_unicode_and_spaces_and_dash_C(self):
        self.repo = os.path.join(self.tmp, "Projekt ü 名 sp")
        self.mg("init", self.repo, cwd=self.tmp)
        write(self.p("sub dir/f.txt"), b"f\n")
        self.mg("-C", self.repo, "add", ".", cwd=self.tmp)
        self.mg("-C", os.path.join(self.repo, "sub dir"), "commit", "-m", "c", cwd=self.tmp)
        self.assertEqual(self.mg("-C", self.repo, "ls-files", cwd=self.tmp).out, b"sub dir/f.txt\n")
        self.assertEqual(self.porcelain(), [])


# --------------------------------------------------------------------------- deep dirs / long paths

class DeepDirs(Base):

    def test_switch_and_rm_prune_empty_deep_directories(self):
        self.base_repo()
        self.mg("branch", "flat")
        write(self.p("d1/d2/d3/d4/d5/d6/f.txt"), b"deep\n")
        write(self.p("ü dir/名/x.txt"), b"u\n")
        self.commit_all("deep")
        self.mg("switch", "flat")
        self.assertFalse(os.path.exists(self.p("d1")), "empty dirs left behind by switch")
        self.assertFalse(os.path.exists(self.p("ü dir")))
        self.mg("switch", "main")
        self.assertEqual(read(self.p("d1/d2/d3/d4/d5/d6/f.txt")), b"deep\n")
        self.mg("rm", "-r", "ü dir")
        self.assertFalse(os.path.exists(self.p("ü dir")), "rm -r left an empty dir")
        self.mg("reset", "--hard", "flat")
        self.assertFalse(os.path.exists(self.p("d1")), "reset --hard left empty dirs")
        self.assertEqual(sorted(os.listdir(self.repo)), [".minigit", "base.txt"])

    def test_path_longer_than_260_chars(self):
        self.init_repo()
        rel = "/".join("d%02d_%s" % (i, "x" * 20) for i in range(15)) + "/leaf.txt"
        try:
            write(self.p(rel), b"leaf\n")
        except OSError as exc:
            self.skipTest(f"OS cannot create long paths here: {exc}")
        self.assertGreater(len(self.p(rel)), 260)
        self.commit_all("long")
        self.assertEqual(self.ls_files(), [rel])
        shutil.rmtree(self.p("d00_" + "x" * 20))
        self.assertEqual(self.porcelain(), [" D " + rel])
        self.mg("reset", "--hard")
        self.assertEqual(read(self.p(rel)), b"leaf\n")
        self.assertEqual(self.porcelain(), [])


# --------------------------------------------------------------------------- deleted / renamed files

class DeletedRenamed(Base):

    def test_rename_and_staged_delete_with_file_on_disk(self):
        self.init_repo()
        write(self.p("old name.bin"), b"\0bin")
        write(self.p("keep.txt"), b"k\n")
        self.commit_all()
        os.rename(self.p("old name.bin"), self.p("new name.bin"))
        self.assertEqual(sorted(self.porcelain()), [" D old name.bin", "?? new name.bin"])
        self.mg("add", ".")
        self.assertEqual(sorted(self.porcelain()), ["A  new name.bin", "D  old name.bin"])
        self.mg("commit", "-m", "rename")
        self.mg("rm", "--cached", "keep.txt")
        self.assertEqual(sorted(self.porcelain()), ["?? keep.txt", "D  keep.txt"])
        self.assertTrue(os.path.exists(self.p("keep.txt")))

    def test_add_of_deleted_directory_removes_entries_and_restore_brings_back(self):
        self.init_repo()
        write(self.p("dd/a.txt"), b"1\n")
        write(self.p("dd/sub/b.txt"), b"2\n")
        self.commit_all()
        shutil.rmtree(self.p("dd"))
        self.assertEqual(self.porcelain(), [" D dd/a.txt", " D dd/sub/b.txt"])
        self.mg("restore", "dd")
        self.assertEqual(read(self.p("dd/sub/b.txt")), b"2\n")
        shutil.rmtree(self.p("dd"))
        self.mg("add", "dd")
        self.assertEqual(self.porcelain(), ["D  dd/a.txt", "D  dd/sub/b.txt"])

    def test_commit_a_records_deletions(self):
        self.init_repo()
        write(self.p("a.txt"), b"a\n")
        write(self.p("gone/ü.txt"), b"g\n")
        self.commit_all()
        os.remove(self.p("gone/ü.txt"))
        write(self.p("a.txt"), b"a2\n")
        self.mg("commit", "-am", "del")
        self.assertEqual(self.ls_files(), ["a.txt"])
        r = self.mg("show", "HEAD:gone/ü.txt", check=False)
        self.assertEqual(r.code, 1, str(r))
        self.assertIn("deleted file mode 100644".encode(), self.mg("show", "HEAD").out)

    def test_file_replaced_by_directory_and_back(self):
        self.init_repo()
        write(self.p("x"), b"file\n")
        self.commit_all("file")
        self.mg("branch", "as-file")
        os.remove(self.p("x"))
        write(self.p("x/y"), b"dir\n")
        self.assertEqual(self.porcelain(), [" D x", "?? x/y"])
        self.mg("add", ".")
        self.assertEqual(self.porcelain(), ["D  x", "A  x/y"])
        self.mg("commit", "-m", "dir")
        self.mg("switch", "as-file")
        self.assertTrue(os.path.isfile(self.p("x")))
        self.assertEqual(read(self.p("x")), b"file\n")
        self.mg("switch", "main")
        self.assertTrue(os.path.isdir(self.p("x")))
        self.assertEqual(read(self.p("x/y")), b"dir\n")
        self.assertEqual(self.porcelain(), [])

    def test_case_only_rename_does_not_create_duplicate_entries(self):
        self.init_repo()
        if not fs_is_case_insensitive(self.repo):
            self.skipTest("case-sensitive filesystem")
        write(self.p("readme.txt"), b"r\n")
        self.commit_all()
        os.rename(self.p("readme.txt"), self.p("README.txt"))
        self.mg("add", ".")
        self.mg("commit", "-m", "case rename", check=False)
        names = self.ls_files()
        self.assertEqual(len({n.lower() for n in names}), len(names),
                         f"index holds names that collide on this filesystem: {names}")

    def test_add_with_other_case_updates_existing_entry(self):
        self.init_repo()
        if not fs_is_case_insensitive(self.repo):
            self.skipTest("case-sensitive filesystem")
        write(self.p("readme.txt"), b"v1\n")
        self.commit_all()
        write(self.p("readme.txt"), b"v2\n")
        self.mg("add", "README.TXT")
        names = self.ls_files()
        self.assertEqual(names, ["readme.txt"], f"duplicate entry created: {names}")
        self.assertEqual(self.ls_stage()[("readme.txt", "0")][1], blob_sha(b"v2\n"))

    def _case_rename(self, old: str, new: str) -> None:
        os.rename(self.p(old), self.p("case-rename-tmp"))
        os.rename(self.p("case-rename-tmp"), self.p(new))

    def test_case_only_rename_keeps_one_entry_and_a_clean_status(self):
        self.init_repo()
        if not fs_is_case_insensitive(self.repo):
            self.skipTest("case-sensitive filesystem")
        write(self.p("Readme.md"), b"a\n")
        self.commit_all()
        self._case_rename("Readme.md", "README.md")
        self.assertEqual(self.porcelain(), [])
        self.mg("add", ".")
        self.assertEqual(self.ls_files(), ["Readme.md"])
        write(self.p("README.md"), b"changed\n")
        self.mg("add", "README.md")
        self.mg("commit", "-m", "edit")
        self.assertEqual(self.ls_files(), ["Readme.md"])
        self.assertEqual(self.porcelain(), [])
        self.mg("rm", "-f", "Readme.md")
        self.assertEqual(self.ls_files(), [])
        self.assertEqual(self.porcelain(), ["D  Readme.md"])

    def test_add_new_file_with_other_case_directory_uses_one_spelling(self):
        self.init_repo()
        if not fs_is_case_insensitive(self.repo):
            self.skipTest("case-sensitive filesystem")
        write(self.p("sub/c.txt"), b"c\n")
        self.mg("add", "sub/c.txt")
        self.mg("add", "SUB/C.TXT")
        write(self.p("sub/d.txt"), b"d\n")
        self.mg("add", "SUB/D.TXT")
        self.assertEqual(self.ls_files(), ["sub/c.txt", "sub/d.txt"])
        self.mg("commit", "-m", "case")
        commit = self.mg("cat-file", "-p", "HEAD").out.decode()
        tree = commit.split("\n", 1)[0].split()[1]
        root = self.mg("cat-file", "-p", tree).out.decode()
        self.assertEqual([ln.split("\t")[1] for ln in root.splitlines()], ["sub"], root)

    def _two_case_branches(self, same_content: bool) -> None:
        write(self.p("README.md"), b"hi\n")
        write(self.p("other.txt"), b"o\n")
        self.commit_all("base")
        self.mg("switch", "-c", "lower")
        self.mg("rm", "--cached", "README.md")
        self._case_rename("README.md", "readme.md")
        if not same_content:
            write(self.p("readme.md"), b"lower\n")
        self.mg("add", "readme.md")
        self.mg("commit", "-m", "lower")
        self.assertEqual(self.porcelain(), [])

    def test_switch_and_reset_across_case_only_rename_keep_the_file(self):
        for same in (True, False):
            with self.subTest(same_content=same):
                self.init_repo(f"repo-{same}")
                if not fs_is_case_insensitive(self.repo):
                    self.skipTest("case-sensitive filesystem")
                self._two_case_branches(same)
                self.mg("switch", "main")
                self.assertIn("README.md", os.listdir(self.repo))
                self.assertEqual(read(self.p("README.md")), b"hi\n")
                self.assertEqual(self.porcelain(), [])
                self.mg("switch", "lower")
                self.assertIn("readme.md", os.listdir(self.repo))
                self.assertEqual(self.porcelain(), [])
                self.mg("switch", "-c", "probe")
                self.mg("reset", "--hard", "main")
                self.assertIn("README.md", os.listdir(self.repo))
                self.assertEqual(read(self.p("README.md")), b"hi\n")
                self.assertEqual(self.porcelain(), [])

    def test_checkout_of_case_colliding_tree_is_refused_cleanly(self):
        self.init_repo()
        if not fs_is_case_insensitive(self.repo):
            self.skipTest("case-sensitive filesystem")
        write(self.p("base.txt"), b"base\n")
        self.commit_all("base")
        base = self.head_sha()
        tree = write_tree(self.gitdir, [("100644", "README.txt", write_object(self.gitdir, "blob", b"A\n")),
                                        ("100644", "base.txt", write_object(self.gitdir, "blob", b"new\n")),
                                        ("100644", "readme.txt", write_object(self.gitdir, "blob", b"a\n"))])
        c = write_commit(self.gitdir, tree, [base])
        for args in (("checkout", c), ("merge", c), ("reset", "--hard", c)):
            with self.subTest(args=args):
                r = self.mg(*args, check=False)
                self.assertEqual(r.code, 1, str(r))
                self.assertIn(b"'README.txt' and 'readme.txt'", r.err)
                self.assertEqual(self.head_sha(), base)
                self.assertEqual(sorted(os.listdir(self.repo)), [".minigit", "base.txt"])
                self.assertEqual(read(self.p("base.txt")), b"base\n")
                self.assertEqual(self.porcelain(), [])


# --------------------------------------------------------------------------- Windows paths & modes

class WindowsPaths(Base):

    @unittest.skipUnless(IS_WIN, "backslash is a separator only on Windows")
    def test_backslash_relative_paths_from_subdirectory(self):
        self.init_repo()
        write(self.p("top.txt"), b"top\n")
        write(self.p("s1/s2/in.txt"), b"in\n")
        self.commit_all()
        write(self.p("top.txt"), b"top2\n")
        write(self.p("s1/s2/in.txt"), b"in2\n")
        s1 = self.p("s1")
        self.mg("add", "s2\\in.txt", "..\\top.txt", cwd=s1)
        self.assertEqual(self.porcelain(), ["M  s1/s2/in.txt", "M  top.txt"])
        self.mg("restore", "--staged", ".\\s2\\in.txt", cwd=s1)
        self.assertIn(b"diff --git a/s1/s2/in.txt b/s1/s2/in.txt", self.mg("diff", "--", "s2\\in.txt", cwd=s1).out)
        self.mg("rm", "-f", "..\\top.txt", cwd=s1)
        self.assertEqual(self.porcelain(), [" M s1/s2/in.txt", "D  top.txt"])
        self.mg("-C", "s1\\s2", "add", "in.txt", cwd=self.repo)
        self.assertEqual(self.porcelain(), ["M  s1/s2/in.txt", "D  top.txt"])

    def test_absolute_paths_inside_and_outside_repo(self):
        self.init_repo()
        write(self.p("abs.txt"), b"a\n")
        write(self.p("abs2.txt"), b"b\n")
        write(os.path.join(self.tmp, "outside.txt"), b"o\n")
        self.mg("add", os.path.abspath(self.p("abs.txt")))
        p2 = os.path.abspath(self.p("abs2.txt"))
        if IS_WIN:
            p2 = p2[0].lower() + p2[1:]          # e: vs E:
            p2 = p2.replace("\\", "/")            # forward slashes are valid on Windows
        self.mg("add", p2)
        self.assertEqual(self.ls_files(), ["abs.txt", "abs2.txt"])
        for bad in (os.path.join(self.tmp, "outside.txt"), os.path.join("..", "outside.txt")):
            r = self.mg("add", bad, check=False)
            self.assertEqual(r.code, 1, str(r))
            self.assertIn("outside repository", r.text)
        self.assertEqual(self.ls_files(), ["abs.txt", "abs2.txt"])

    def _commit_with_exec_script(self):
        base = self.base_repo()
        b1 = blob_sha(b"base\n")
        b2 = write_object(self.gitdir, "blob", b"#!/bin/sh\necho hi\n")
        tree = write_tree(self.gitdir, [("100644", "base.txt", b1), ("100755", "run.sh", b2)])
        c = write_commit(self.gitdir, tree, [base])
        self.mg("reset", "--hard", c)
        self.assertEqual(self.ls_stage()[("run.sh", "0")][0], "100755")
        return c

    def test_exec_bit_kept_when_file_is_edited(self):
        self._commit_with_exec_script()
        self.assertEqual(self.porcelain(), [])
        write(self.p("run.sh"), b"#!/bin/sh\necho changed\n")
        self.mg("add", "run.sh")
        self.assertEqual(self.ls_stage()[("run.sh", "0")][0], "100755")
        write(self.p("run.sh"), b"#!/bin/sh\necho again\n")
        self.mg("commit", "-am", "again")
        self.assertIn(b"100755 blob", self.mg("cat-file", "-p", "HEAD:").out)

    def test_exec_bit_kept_after_rm_cached_and_readd(self):
        """SPEC 6: on Windows the mode comes from the previous index *or HEAD* entry."""
        self._commit_with_exec_script()
        self.mg("rm", "--cached", "run.sh")
        self.mg("add", "run.sh")
        self.assertEqual(self.ls_stage()[("run.sh", "0")][0], "100755",
                         f"exec bit dropped; status: {self.porcelain()}")
        self.assertEqual(self.porcelain(), [])

    def test_hostile_tree_names_refused_without_touching_anything(self):
        """SPEC 2: names with ':' '\\' '..' and gitdir aliases are rejected on every platform."""
        hostile = ["a:b", "C:", "a\\b", "..", ".MINIGIT/HEAD", ".minigit./HEAD", ".minigit /HEAD",
                   ".minigit.. ./HEAD", "MINIGI~1/HEAD", "minigi~9/config", "sub/.MiniGit/config",
                   "ok/stream.txt:evil"]
        base = self.base_repo()
        for name in hostile:
            with self.subTest(name=name):
                blob = write_object(self.gitdir, "blob", b"payload\n")
                top, sha, mode = nested_tree(self.gitdir, name, blob)
                tree = write_tree(self.gitdir, [("100644", "base.txt", blob_sha(b"base\n")), (mode, top, sha)])
                c = write_commit(self.gitdir, tree, [base])
                r = self.mg("checkout", c, check=False)
                self.assertEqual(r.code, 1, str(r))
                self.assertNotIn("internal error", r.text)
                self.assertEqual(self.head_raw(), "ref: refs/heads/main")
                self.assertEqual(sorted(os.listdir(self.repo)), [".minigit", "base.txt"])
                self.assertEqual(read(os.path.join(self.gitdir, "HEAD")), b"ref: refs/heads/main\n")
                self.assertEqual(self.porcelain(), [])

    def test_failed_reset_to_hostile_tree_does_not_move_head(self):
        base = self.base_repo()
        blob = write_object(self.gitdir, "blob", b"payload\n")
        tree = write_tree(self.gitdir, [("100644", "a:b", blob)])
        bad = write_commit(self.gitdir, tree, [base])
        for mode in ("--hard", "--mixed"):
            with self.subTest(mode=mode):
                r = self.mg("reset", mode, bad, check=False)
                self.assertEqual(r.code, 1, str(r))
                self.assertEqual(read(os.path.join(self.gitdir, "refs", "heads", "main")).decode().strip(), base,
                                 f"reset {mode} failed but moved the branch to the invalid commit")
                st = self.mg("status", "--porcelain", check=False)
                self.assertEqual(st.code, 0, f"repository is wedged after failed reset:\n{st}")
                # recover for the next subtest
                write(os.path.join(self.gitdir, "refs", "heads", "main"), base + "\n")

    @unittest.skipUnless(IS_WIN, "Windows reserved device names / trailing dots")
    def test_windows_unrepresentable_names_refused_or_consistent(self):
        """A Linux user can commit 'NUL', 'trail.' or 'trail '; git for Windows refuses such paths.

        Accept either outcome: checkout refused with nothing touched, or a checkout after which the
        status is clean and switching back works. Silently writing into a device / another name is a bug.
        """
        for name in ("NUL", "trail.", "trail "):
            with self.subTest(name=name):
                td = tempfile.mkdtemp(prefix="mg qa win ", dir=self.tmp)
                self.repo = os.path.join(td, "r")
                os.makedirs(self.repo)
                self.mg("init")
                write(self.p("base.txt"), b"base\n")
                self.commit_all("base")
                base = self.head_sha()
                blob = write_object(self.gitdir, "blob", b"precious data\n")
                tree = write_tree(self.gitdir, [("100644", "base.txt", blob_sha(b"base\n")), ("100644", name, blob)])
                c = write_commit(self.gitdir, tree, [base])
                r = self.mg("checkout", c, check=False)
                if r.code != 0:
                    self.assertEqual(self.head_raw(), "ref: refs/heads/main")
                    self.assertEqual(sorted(os.listdir(self.repo)), [".minigit", "base.txt"])
                    continue
                st = self.porcelain()
                self.assertEqual(st, [], f"checkout of {name!r} reported success but status is {st}")
                back = self.mg("switch", "main", check=False)
                self.assertEqual(back.code, 0, f"cannot switch back after checking out {name!r}:\n{back}")

    @unittest.skipUnless(IS_WIN, "these characters are legal on Linux but not on Windows")
    def test_windows_invalid_characters_do_not_half_apply_a_merge(self):
        """Fast-forward to a commit holding 'what?.txt' (legal on Linux) must not half-update the tree."""
        for name in ("what?.txt", 'q"uote.txt', "pi|pe.txt", "lt<gt>.txt"):
            with self.subTest(name=name):
                td = tempfile.mkdtemp(prefix="mg qa win ", dir=self.tmp)
                self.repo = os.path.join(td, "r")
                os.makedirs(self.repo)
                self.mg("init")
                write(self.p("base.txt"), b"base\n")
                self.commit_all("base")
                base = self.head_sha()
                b_new = write_object(self.gitdir, "blob", b"upstream change\n")
                b_odd = write_object(self.gitdir, "blob", b"odd\n")
                tree = write_tree(self.gitdir, [("100644", "base.txt", b_new), ("100644", name, b_odd)])
                c = write_commit(self.gitdir, tree, [base])
                r = self.mg("merge", c, check=False)
                if r.code == 0:
                    self.assertEqual(self.porcelain(), [])
                    continue
                self.assertNotIn("internal error", r.text)
                self.assertEqual(self.head_sha(), base)
                self.assertEqual(read(self.p("base.txt")), b"base\n",
                                 "merge failed but already rewrote base.txt (half-applied)")
                self.assertEqual(self.porcelain(), [], "failed merge left phantom local changes")

    def test_readonly_tracked_file_switch_and_rm(self):
        self.init_repo()
        write(self.p("ro.txt"), b"v1\n")
        self.commit_all("one")
        self.mg("switch", "-c", "b2")
        write(self.p("ro.txt"), b"v2\n")
        self.mg("commit", "-am", "two")
        self.mg("switch", "main")
        os.chmod(self.p("ro.txt"), stat.S_IREAD)
        self.mg("switch", "b2")
        self.assertEqual(read(self.p("ro.txt")), b"v2\n")
        os.chmod(self.p("ro.txt"), stat.S_IREAD)
        self.mg("rm", "ro.txt")
        self.assertFalse(os.path.exists(self.p("ro.txt")))

    def test_racy_same_size_rewrite_with_same_mtime_is_detected(self):
        self.init_repo()
        write(self.p("r.txt"), b"aaaa\n")
        self.mg("add", "r.txt")
        st = os.stat(self.p("r.txt"))
        write(self.p("r.txt"), b"bbbb\n")
        os.utime(self.p("r.txt"), ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.porcelain(), ["AM r.txt"])
        self.mg("commit", "-am", "racy")
        self.assertEqual(self.mg("show", "HEAD:r.txt").out, b"bbbb\n")


# --------------------------------------------------------------------------- through the server

class ThroughServer(Base):

    def start_server(self):
        root = os.path.join(self.tmp, "srv root")
        self.mg("server", "init", root, cwd=self.tmp)
        token = self.mg("server", "add-user", root, "alice", cwd=self.tmp).out.decode().strip()
        self.mg("server", "create-repo", root, "proj", cwd=self.tmp)
        ready = os.path.join(self.tmp, "ready.txt")
        log = open(os.path.join(self.tmp, "server.log"), "ab")
        proc = subprocess.Popen([sys.executable, "-m", "minigit", "serve", "--root", root, "--port", "0",
                                 "--ready-file", ready, "--quiet"], cwd=self.tmp, env=self.env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

        def stop():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
            log.close()
        self.addCleanup(stop)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail("server exited early")
            try:
                line = read(ready).decode().strip()
            except OSError:
                line = ""
            if line:
                break
            time.sleep(0.05)
        else:
            self.fail("server did not become ready")
        self.env["MINIGIT_USER"] = "alice"
        self.env["MINIGIT_TOKEN"] = token
        return "http://" + line, token

    def info_refs(self, url, token):
        req = urllib.request.Request(url + "/proj/info/refs", headers={
            "X-Minigit-Protocol": "1",
            "Authorization": "Basic " + base64.b64encode(f"alice:{token}".encode()).decode()})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read())

    def test_binary_crlf_unicode_and_large_files_survive_push_clone_pull(self):
        url, _ = self.start_server()
        self.mg("clone", url + "/proj", "alice", cwd=self.tmp)
        self.repo = os.path.join(self.tmp, "alice")
        files = {
            "crlf.txt": b"a\r\nb\r\nc",
            "bin.dat": bytes(range(256)) + b"\r\n\n\0",
            "dir sp/ünï/日本/fïlé 名.txt": "ü\r\n".encode(),
            "big.bin": os.urandom(6 * 1024 * 1024),
            "empty": b"",
        }
        for rel, data in files.items():
            write(self.p(rel), data)
        self.commit_all("files")
        self.mg("push", "-u", "origin", "main", timeout=180)
        dest = os.path.join(self.tmp, "clone ü 名")
        self.mg("clone", url + "/proj", dest, cwd=self.tmp, timeout=180)
        for rel, data in files.items():
            self.assertEqual(read(os.path.join(dest, *rel.split("/"))), data, rel)
        self.assertEqual(self.mg("status", "--porcelain", cwd=dest).out, b"")
        self.mg("fsck", cwd=dest)
        # change from the clone, pull back into alice (fast-forward)
        write(os.path.join(dest, "crlf.txt"), b"a\r\nB\r\nc\r\n")
        write(os.path.join(dest, "bin.dat"), b"\0new\xff")
        os.remove(os.path.join(dest, "dir sp", "ünï", "日本", "fïlé 名.txt"))
        self.mg("commit", "-am", "edit", cwd=dest)
        self.mg("push", cwd=dest, timeout=180)
        self.mg("pull", timeout=180)
        self.assertEqual(read(self.p("crlf.txt")), b"a\r\nB\r\nc\r\n")
        self.assertEqual(read(self.p("bin.dat")), b"\0new\xff")
        self.assertFalse(os.path.exists(self.p("dir sp")), "deleted file's empty dirs not pruned by pull")
        self.assertEqual(self.porcelain(), [])

    def test_push_of_tree_with_windows_hostile_name_is_rejected(self):
        url, token = self.start_server()
        self.mg("clone", url + "/proj", "alice", cwd=self.tmp)
        self.repo = os.path.join(self.tmp, "alice")
        write(self.p("ok.txt"), b"ok\n")
        self.commit_all("ok")
        self.mg("push", "-u", "origin", "main")
        before = self.info_refs(url, token)["refs"]
        blob = write_object(self.gitdir, "blob", b"evil\n")
        tree = write_tree(self.gitdir, [("100644", "ok.txt", blob_sha(b"ok\n")), ("100644", "a:b", blob)])
        c = write_commit(self.gitdir, tree, [self.head_sha()])
        r = self.mg("push", "origin", f"{c}:evil", check=False)
        self.assertEqual(r.code, 1, str(r))
        self.assertNotIn("internal error", r.text)
        self.assertEqual(self.info_refs(url, token)["refs"], before)


if __name__ == "__main__":
    unittest.main()

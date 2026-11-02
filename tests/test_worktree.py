"""tests.test_worktree -- owner: M2 worktree (index, ignore rules, worktree scanning, checkout_tree)."""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
import time
import unittest
from unittest import mock

from minigit import errors
from minigit import index as idx
from minigit import worktree as wt
from minigit.ignore import IgnoreMatcher, parse_pattern
from minigit.index import Index, IndexEntry, locked_index, read_index

IS_WINDOWS = os.name == "nt"


# ----------------------------------------------------------------------------- fakes
def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FakeStore:
    """Minimal ObjectStore stand-in (dict based); M2 only needs write/read_typed/has."""

    def __init__(self) -> None:
        self.objs: dict[str, tuple[str, bytes]] = {}

    def write(self, obj_type: str, data: bytes) -> str:
        sha = hashlib.sha1(b"%s %d\0" % (obj_type.encode(), len(data)) + data).hexdigest()
        self.objs[sha] = (obj_type, data)
        return sha

    def has(self, sha: str) -> bool:
        return sha in self.objs

    def read_typed(self, sha: str, obj_type: str) -> bytes:
        t, data = self.objs[sha]
        assert t == obj_type
        return data


class FakeRepo:
    """Repository stand-in with the attributes M2 uses (SPEC section 8, M1 repo.py)."""

    def __init__(self, root: str) -> None:
        self.worktree = root
        self.gitdir = os.path.join(root, ".minigit")
        self.bare = False
        os.makedirs(os.path.join(self.gitdir, "objects"), exist_ok=True)
        self.objects = FakeStore()

    def gpath(self, *parts: str) -> str:
        return os.path.join(self.gitdir, *parts)

    def abspath(self, rel: str) -> str:
        return os.path.join(self.worktree, *rel.split("/")) if rel else self.worktree

    def require_worktree(self) -> str:
        return self.worktree


class TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.realpath(self._tmp.name)
        self.repo = FakeRepo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, rel: str) -> str:
        return self.repo.abspath(rel)

    def write(self, rel: str, data: bytes, mtime_ns: int | None = None) -> str:
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        if mtime_ns is not None:
            os.utime(p, ns=(mtime_ns, mtime_ns))
        return p

    def read(self, rel: str) -> bytes:
        with open(self.path(rel), "rb") as f:
            return f.read()

    def blob(self, data: bytes, mode: str = "100644") -> tuple[str, str]:
        return (mode, self.repo.objects.write("blob", data))

    def entry(self, path: str, data: bytes = b"x", stage: int = 0, mode: str = "100644") -> IndexEntry:
        return IndexEntry(path, mode, blob_sha(data), len(data), 0, stage)


# ----------------------------------------------------------------------------- index
class IndexTests(TmpCase):
    def sample(self) -> Index:
        ix = Index()
        ix.put(IndexEntry("b.txt", "100644", blob_sha(b"b"), 1, 123456789, 0))
        ix.put(IndexEntry("a/x.sh", "100755", blob_sha(b"#!"), 2, 5, 0))
        ix.put(IndexEntry("café/ü.txt", "100644", blob_sha(b""), 0, 0, 0))
        ix.put(IndexEntry("bad\udcff.bin", "100644", blob_sha(b"z"), 1, 1, 0))  # surrogateescape path
        for s in (1, 2, 3):
            ix.put(IndexEntry("c.txt", "100644", blob_sha(b"%d" % s), 1, 0, s))
        return ix

    def test_round_trip(self) -> None:
        ix = self.sample()
        data = ix.serialize()
        self.assertEqual(data[:4], b"MIDX")
        self.assertEqual(struct.unpack(">4sII", data[:12])[1:], (1, len(ix.entries())))
        self.assertEqual(hashlib.sha1(data[:-20]).digest(), data[-20:])
        back = Index.parse(data)
        self.assertEqual(back.entries(), ix.entries())
        self.assertEqual(back.serialize(), data)

    def test_load_missing_is_empty(self) -> None:
        ix = Index.load(self.repo.gpath("index"))
        self.assertEqual(ix.entries(), [])
        self.assertEqual(ix.file_mtime_ns, 0)
        self.assertEqual(read_index(self.repo).entries(), [])
        self.assertEqual(idx.index_path(self.repo), self.repo.gpath("index"))

    def test_load_from_file_records_mtime(self) -> None:
        ix = self.sample()
        p = self.repo.gpath("index")
        with open(p, "wb") as f:
            f.write(ix.serialize())
        loaded = read_index(self.repo)
        self.assertEqual(loaded.entries(), ix.entries())
        self.assertEqual(loaded.file_mtime_ns, os.stat(p).st_mtime_ns)

    def test_entries_sorted_by_path_then_stage(self) -> None:
        ix = self.sample()
        keys = [(e.path, e.stage) for e in ix.entries()]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(ix.paths(), sorted({k[0] for k in keys}))

    def test_stage_handling(self) -> None:
        ix = self.sample()
        self.assertEqual(sorted(ix.unmerged()["c.txt"]), [1, 2, 3])
        self.assertNotIn("c.txt", ix.flat())
        self.assertNotIn("c.txt", ix.stage0())
        self.assertIn("c.txt", ix.paths())
        # resolving with a stage-0 entry clears stages 1-3
        ix.put(IndexEntry("c.txt", "100644", blob_sha(b"r"), 1, 0, 0))
        self.assertEqual(ix.unmerged(), {})
        self.assertEqual(ix.flat()["c.txt"], ("100644", blob_sha(b"r")))
        # putting a conflict stage clears stage 0
        ix.put(IndexEntry("b.txt", "100644", blob_sha(b"o"), 1, 0, 2))
        self.assertIsNone(ix.get("b.txt"))
        self.assertEqual(list(ix.unmerged()["b.txt"]), [2])
        self.assertIsNotNone(ix.get("b.txt", 2))

    def test_remove_all_stages(self) -> None:
        ix = self.sample()
        self.assertTrue(ix.remove("c.txt"))
        self.assertNotIn("c.txt", ix.paths())
        self.assertFalse(ix.remove("c.txt"))
        self.assertFalse(ix.remove("nope"))

    def test_flat_and_stage0(self) -> None:
        ix = self.sample()
        flat = ix.flat()
        self.assertEqual(flat["a/x.sh"], ("100755", blob_sha(b"#!")))
        self.assertEqual(set(flat), set(ix.stage0()))
        self.assertIsInstance(ix.stage0()["b.txt"], IndexEntry)

    def test_put_rejects_bad_entries(self) -> None:
        ix = Index()
        for bad in (IndexEntry("a", "40000", "0" * 40), IndexEntry("a", "100644", "xyz"),
                    IndexEntry("", "100644", "0" * 40), IndexEntry("a", "100644", "0" * 40, stage=4),
                    IndexEntry("a\\b", "100644", "0" * 40), IndexEntry("/a", "100644", "0" * 40)):
            with self.assertRaises(ValueError):
                ix.put(bad)

    def assertCorrupt(self, data: bytes) -> None:
        with self.assertRaises(errors.MinigitError) as cm:
            Index.parse(data)
        self.assertIn("index file is corrupt; run 'minigit reset' to rebuild it from HEAD", str(cm.exception))

    def test_corrupt_trailer(self) -> None:
        data = bytearray(self.sample().serialize())
        data[-1] ^= 0xFF
        self.assertCorrupt(bytes(data))

    def test_corrupt_body_flip(self) -> None:
        data = bytearray(self.sample().serialize())
        data[20] ^= 0x01
        self.assertCorrupt(bytes(data))

    def test_corrupt_magic_version_length(self) -> None:
        def framed(body: bytes) -> bytes:
            return body + hashlib.sha1(body).digest()
        self.assertCorrupt(framed(struct.pack(">4sII", b"XIDX", 1, 0)))
        self.assertCorrupt(framed(struct.pack(">4sII", b"MIDX", 2, 0)))
        self.assertCorrupt(framed(struct.pack(">4sII", b"MIDX", 1, 3)))           # count too large
        self.assertCorrupt(framed(struct.pack(">4sII", b"MIDX", 1, 0) + b"junk"))  # trailing bytes
        self.assertCorrupt(b"")
        self.assertCorrupt(b"MIDX")
        good = self.sample().serialize()
        self.assertCorrupt(good[:-25] + good[-20:])  # truncated

    def test_corrupt_index_file_via_load(self) -> None:
        with open(self.repo.gpath("index"), "wb") as f:
            f.write(b"garbage" * 10)
        with self.assertRaises(errors.MinigitError):
            read_index(self.repo)

    def test_is_racy(self) -> None:
        ix = Index(file_mtime_ns=10_000_000_000)
        self.assertTrue(ix.is_racy(IndexEntry("a", "100644", "0" * 40, 1, 0)))
        self.assertTrue(ix.is_racy(IndexEntry("a", "100644", "0" * 40, 1, 8_000_000_000)))
        self.assertTrue(ix.is_racy(IndexEntry("a", "100644", "0" * 40, 1, 9_999_999_999)))
        self.assertFalse(ix.is_racy(IndexEntry("a", "100644", "0" * 40, 1, 7_999_999_999)))
        self.assertTrue(Index().is_racy(IndexEntry("a", "100644", "0" * 40, 1, 5)))  # no file -> racy

    def test_locked_index_writes_on_clean_exit(self) -> None:
        with locked_index(self.repo) as ix:
            ix.put(self.entry("hello.txt", b"hi"))
            self.assertTrue(os.path.exists(self.repo.gpath("index.lock")))
        self.assertFalse(os.path.exists(self.repo.gpath("index.lock")))
        self.assertEqual(list(read_index(self.repo).flat()), ["hello.txt"])
        self.assertEqual(ix.file_mtime_ns, os.stat(self.repo.gpath("index")).st_mtime_ns)

    def test_locked_index_discards_on_error(self) -> None:
        with locked_index(self.repo) as ix:
            ix.put(self.entry("keep.txt"))
        with self.assertRaises(RuntimeError):
            with locked_index(self.repo) as ix:
                ix.put(self.entry("lost.txt"))
                raise RuntimeError("boom")
        self.assertFalse(os.path.exists(self.repo.gpath("index.lock")))
        self.assertEqual(list(read_index(self.repo).flat()), ["keep.txt"])

    def test_locked_index_held_lock(self) -> None:
        with locked_index(self.repo):
            with self.assertRaises(errors.LockError):
                with locked_index(self.repo):
                    pass
        with locked_index(self.repo):  # released again
            pass


# ----------------------------------------------------------------------------- ignore
class IgnoreTests(TmpCase):
    def matcher(self, root_rules: str, nested: dict[str, str] | None = None,
                tracked=(), extra=()) -> IgnoreMatcher:
        self.write(".minigitignore", root_rules.encode())
        for d, rules in (nested or {}).items():
            self.write(d + "/.minigitignore", rules.encode())
        return IgnoreMatcher(self.root, extra, tracked=tracked)

    def check(self, m: IgnoreMatcher, table: list[tuple[str, bool, bool]]) -> None:
        for path, is_dir, expected in table:
            with self.subTest(path=path, is_dir=is_dir):
                self.assertEqual(m.is_ignored(path, is_dir), expected)

    def test_basename_any_depth(self) -> None:
        m = self.matcher("*.log\n")
        self.check(m, [("a.log", False, True), ("x/y/a.log", False, True), ("a.txt", False, False),
                       ("logs", True, False), ("x.log/inner.txt", False, True)])

    def test_anchored_leading_slash(self) -> None:
        m = self.matcher("/build\n")
        self.check(m, [("build", True, True), ("build", False, True), ("build/x.o", False, True),
                       ("src/build", True, False), ("src/build/x.o", False, False)])

    def test_inner_slash_anchors(self) -> None:
        m = self.matcher("doc/*.txt\n")
        self.check(m, [("doc/a.txt", False, True), ("doc/sub/a.txt", False, False),
                       ("x/doc/a.txt", False, False), ("a.txt", False, False)])

    def test_dir_only(self) -> None:
        m = self.matcher("out/\n")
        self.check(m, [("out", True, True), ("out", False, False), ("out/a.txt", False, True),
                       ("src/out", True, True), ("src/out/deep/f", False, True)])

    def test_negation_and_last_match_wins(self) -> None:
        m = self.matcher("*.log\n!keep.log\n")
        self.check(m, [("a.log", False, True), ("keep.log", False, False), ("d/keep.log", False, False)])
        m = self.matcher("!a.txt\na.txt\n")
        self.check(m, [("a.txt", False, True)])

    def test_double_star(self) -> None:
        m = self.matcher("**/foo\na/**/b\nlogs/**\n")
        self.check(m, [("foo", False, True), ("x/y/foo", False, True), ("a/b", False, True),
                       ("a/x/y/b", False, True), ("logs/x/y", False, True), ("logs", False, False),
                       ("c/b", False, False)])

    def test_wildcards_do_not_cross_slash(self) -> None:
        m = self.matcher("x/a*b\nq/?\n")
        self.check(m, [("x/ab", False, True), ("x/aZZb", False, True), ("x/a/b", False, False),
                       ("q/1", False, True), ("q/12", False, False)])

    def test_char_classes(self) -> None:
        m = self.matcher("[abc].c\nfile[0-9].txt\nn[!xy].d\n")
        self.check(m, [("a.c", False, True), ("d.c", False, False), ("file7.txt", False, True),
                       ("filex.txt", False, False), ("nz.d", False, True), ("nx.d", False, False)])

    def test_comments_blank_escapes_crlf(self) -> None:
        m = self.matcher("# comment\r\n\r\n\\#hash\r\n\\!bang\r\ntrail   \r\n")
        self.check(m, [("# comment", False, False), ("#hash", False, True), ("!bang", False, True),
                       ("trail", False, True)])
        self.assertIsNone(parse_pattern("   "))
        self.assertIsNone(parse_pattern("# x"))

    def test_nested_ignore_files(self) -> None:
        m = self.matcher("*.log\n", {"sub": "*.tmp\n/only\n!important.log\n"})
        self.check(m, [("sub/a.tmp", False, True), ("a.tmp", False, False), ("other/a.tmp", False, False),
                       ("sub/x/b.tmp", False, True), ("sub/only", False, True), ("sub/x/only", False, False),
                       ("only", False, False), ("sub/important.log", False, False),
                       ("important.log", False, True), ("sub/a.log", False, True)])

    def test_ignored_directory_not_descended(self) -> None:
        m = self.matcher("build/\n!build/keep.txt\n")
        self.check(m, [("build/keep.txt", False, True), ("build", True, True)])

    def test_minigit_always_ignored(self) -> None:
        m = self.matcher("")
        self.check(m, [(".minigit", True, True), (".minigit/HEAD", False, True), ("a/.MiniGit", True, True),
                       (".minigitignore", False, False), ("", True, False)])

    def test_tracked_but_ignored(self) -> None:
        m = self.matcher("*.log\nbuild/\n", tracked=["a.log", "build/keep.txt"])
        self.check(m, [("a.log", False, False), ("b.log", False, True), ("build", True, False),
                       ("build/keep.txt", False, False), ("build/other.o", False, True)])

    def test_tracked_read_from_index_file(self) -> None:
        ix = Index()
        ix.put(self.entry("a.log"))
        with open(self.repo.gpath("index"), "wb") as f:
            f.write(ix.serialize())
        self.write(".minigitignore", b"*.log\n")
        m = IgnoreMatcher(self.root)
        self.assertFalse(m.is_ignored("a.log"))
        self.assertTrue(m.is_ignored("b.log"))

    def test_extra_patterns_and_backslash_input(self) -> None:
        m = self.matcher("!keep.bak\n", extra=["*.bak"])
        self.check(m, [("x.bak", False, True), ("keep.bak", False, False), ("d\\x.bak", False, True)])

    def test_no_ignore_file(self) -> None:
        m = IgnoreMatcher(self.root)
        self.assertFalse(m.is_ignored("anything.txt"))


# ----------------------------------------------------------------------------- worktree
class ScanTests(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.write(".minigitignore", b"*.log\nbuild/\n")
        for rel in ("a.txt", "dir/b.txt", "dir/deep/c.txt", "x.log", "build/out.o", "dir/y.log"):
            self.write(rel, rel.encode())
        self.write(".minigit/HEAD", b"ref: refs/heads/main\n")

    def test_scan_root(self) -> None:
        files = wt.scan_files(self.repo, IgnoreMatcher(self.root))
        self.assertEqual(list(files), [".minigitignore", "a.txt", "dir/b.txt", "dir/deep/c.txt"])
        self.assertEqual(files["a.txt"].st_size, 5)

    def test_scan_under(self) -> None:
        m = IgnoreMatcher(self.root)
        self.assertEqual(list(wt.scan_files(self.repo, m, "dir")), ["dir/b.txt", "dir/deep/c.txt"])
        self.assertEqual(list(wt.scan_files(self.repo, m, "dir\\deep")), ["dir/deep/c.txt"])
        self.assertEqual(list(wt.scan_files(self.repo, m, "a.txt")), ["a.txt"])
        self.assertEqual(wt.scan_files(self.repo, m, "missing"), {})
        self.assertEqual(wt.scan_files(self.repo, m, "build"), {})
        self.assertEqual(wt.scan_files(self.repo, m, ".minigit"), {})

    def test_scan_includes_tracked_ignored(self) -> None:
        m = IgnoreMatcher(self.root, tracked=["build/out.o", "x.log"])
        files = wt.scan_files(self.repo, m)
        self.assertIn("build/out.o", files)
        self.assertIn("x.log", files)
        self.assertNotIn("dir/y.log", files)

    @unittest.skipIf(IS_WINDOWS, "symlinks need privileges on Windows")
    def test_scan_symlinks(self) -> None:
        os.symlink(self.path("a.txt"), self.path("link.txt"))
        os.symlink(self.path("dir"), self.path("linkdir"))
        os.symlink(self.path("nowhere"), self.path("broken"))
        files = wt.scan_files(self.repo, IgnoreMatcher(self.root))
        self.assertIn("link.txt", files)
        self.assertNotIn("broken", files)
        self.assertFalse(any(p.startswith("linkdir") for p in files))


class ModeAndEntryTests(TmpCase):
    def test_mode_from_stat(self) -> None:
        st_exec = os.stat_result((0o100755, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        st_plain = os.stat_result((0o100644, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        with mock.patch.object(wt, "_IS_WINDOWS", False):
            self.assertEqual(wt.mode_from_stat(st_exec, None), "100755")
            self.assertEqual(wt.mode_from_stat(st_plain, "100755"), "100644")
        with mock.patch.object(wt, "_IS_WINDOWS", True):
            self.assertEqual(wt.mode_from_stat(st_plain, "100755"), "100755")
            self.assertEqual(wt.mode_from_stat(st_exec, None), "100644")
            self.assertEqual(wt.mode_from_stat(st_exec, "100644"), "100644")

    def test_worktree_entry_missing_and_dir(self) -> None:
        ix = Index()
        self.assertIsNone(wt.worktree_entry(self.repo, ix, "nope.txt"))
        os.makedirs(self.path("d"))
        self.assertIsNone(wt.worktree_entry(self.repo, ix, "d"))
        self.write("f", b"1")
        self.assertIsNone(wt.worktree_entry(self.repo, ix, "f/under-a-file"))

    def test_worktree_entry_hashes_untracked(self) -> None:
        self.write("u.txt", b"hello\n")
        self.assertEqual(wt.worktree_entry(self.repo, Index(), "u.txt"),
                         ("100644", "ce013625030ba8dba906f756967f9e9ca394464a"))

    def test_worktree_entry_uses_head_mode_on_windows(self) -> None:
        self.write("u.sh", b"x")
        with mock.patch.object(wt, "_IS_WINDOWS", True):
            self.assertEqual(wt.worktree_entry(self.repo, Index(), "u.sh", head_mode="100755")[0], "100755")

    def test_stat_cache_and_racy_rehash(self) -> None:
        old = time.time_ns() - 60_000_000_000
        self.write("f.txt", b"aaaa", mtime_ns=old)
        ix = Index()
        e = wt.stage_file(self.repo, ix, "f.txt")
        self.assertEqual(e.mtime_ns, old)
        self.assertEqual(e.size, 4)
        ix.file_mtime_ns = time.time_ns()  # index written long after the file: not racy
        # same size + same mtime but different content: the stat cache trusts the entry (no hashing)
        self.write("f.txt", b"bbbb", mtime_ns=old)
        self.assertEqual(wt.worktree_entry(self.repo, ix, "f.txt"), ("100644", blob_sha(b"aaaa")))
        # racy entry (mtime within 2 s of the index mtime) is re-hashed and the change is seen
        ix.file_mtime_ns = old + 1_000_000_000
        self.assertEqual(wt.worktree_entry(self.repo, ix, "f.txt"), ("100644", blob_sha(b"bbbb")))
        # size change is detected without hashing tricks
        ix.file_mtime_ns = time.time_ns()
        self.write("f.txt", b"ccccc", mtime_ns=old)
        self.assertEqual(wt.worktree_entry(self.repo, ix, "f.txt"), ("100644", blob_sha(b"ccccc")))

    def test_racy_after_locked_write(self) -> None:
        # file staged and index written in the same instant: next check must hash
        self.write("g.txt", b"1111")
        with locked_index(self.repo) as ix:
            wt.stage_file(self.repo, ix, "g.txt")
        st = os.stat(self.path("g.txt"))
        self.write("g.txt", b"2222", mtime_ns=st.st_mtime_ns)
        ix2 = read_index(self.repo)
        self.assertTrue(ix2.is_racy(ix2.get("g.txt")))
        self.assertEqual(wt.worktree_entry(self.repo, ix2, "g.txt")[1], blob_sha(b"2222"))

    def test_worktree_flat(self) -> None:
        self.write("a", b"A")
        self.write("b/c", b"C")
        flat = wt.worktree_flat(self.repo, Index(), ["a", "b/c", "missing"])
        self.assertEqual(flat, {"a": ("100644", blob_sha(b"A")), "b/c": ("100644", blob_sha(b"C"))})

    def test_read_worktree_bytes(self) -> None:
        self.write("crlf.txt", b"a\r\nb\r\n\x00\xff")
        self.assertEqual(wt.read_worktree_bytes(self.repo, "crlf.txt"), b"a\r\nb\r\n\x00\xff")
        with self.assertRaises(errors.MinigitError):
            wt.read_worktree_bytes(self.repo, "missing")


class StageTests(TmpCase):
    def test_stage_file_writes_blob_and_entry(self) -> None:
        self.write("sub/x", b"hello\n")
        ix = Index()
        e = wt.stage_file(self.repo, ix, "sub/x")
        self.assertEqual((e.path, e.mode, e.sha, e.size, e.stage),
                         ("sub/x", "100644", "ce013625030ba8dba906f756967f9e9ca394464a", 6, 0))
        self.assertTrue(self.repo.objects.has(e.sha))
        self.assertEqual(ix.get("sub/x"), e)

    def test_stage_file_windows_backslash_path(self) -> None:
        self.write("sub/x", b"data")
        ix = Index()
        e = wt.stage_file(self.repo, ix, "sub\\x")
        self.assertEqual(e.path, "sub/x")
        self.assertEqual(list(ix.flat()), ["sub/x"])
        self.assertEqual(ix.serialize().count(b"sub/x"), 1)
        # the stored path is the POSIX spelling (the binary sha/mtime fields may contain any byte)
        self.assertEqual(ix.paths(), ["sub/x"])
        self.assertNotIn(b"sub\\x", ix.serialize())

    def test_stage_file_clears_conflict_stages_and_keeps_mode(self) -> None:
        self.write("c.sh", b"resolved")
        ix = Index()
        for s in (1, 2, 3):
            ix.put(IndexEntry("c.sh", "100755", blob_sha(b"%d" % s), 1, 0, s))
        with mock.patch.object(wt, "_IS_WINDOWS", True):
            e = wt.stage_file(self.repo, ix, "c.sh")
        self.assertEqual(e.mode, "100755")  # ours' mode carried on Windows
        self.assertEqual(ix.unmerged(), {})
        self.assertEqual(ix.flat()["c.sh"][1], blob_sha(b"resolved"))

    @unittest.skipIf(IS_WINDOWS, "exec bit is POSIX only")
    def test_stage_file_exec_bit(self) -> None:
        p = self.write("run.sh", b"#!/bin/sh\n")
        os.chmod(p, 0o755)
        self.assertEqual(wt.stage_file(self.repo, Index(), "run.sh").mode, "100755")

    def test_stage_file_errors(self) -> None:
        ix = Index()
        with self.assertRaises(errors.MinigitError):
            wt.stage_file(self.repo, ix, "missing.txt")
        os.makedirs(self.path("adir"))
        with self.assertRaises(errors.MinigitError):
            wt.stage_file(self.repo, ix, "adir")
        self.write(".minigit/config", b"")
        with self.assertRaises(errors.MinigitError):
            wt.stage_file(self.repo, ix, ".minigit/config")
        self.assertEqual(ix.entries(), [])

    def test_entry_with_stat(self) -> None:
        p = self.write("e.txt", b"12345")
        e = wt.entry_with_stat(self.repo, "e.txt", "100644", blob_sha(b"12345"))
        st = os.stat(p)
        self.assertEqual((e.size, e.mtime_ns, e.stage), (5, st.st_mtime_ns, 0))
        self.assertEqual(wt.entry_with_stat(self.repo, "gone", "100644", "0" * 40).mtime_ns, 0)


class WriteRemoveTests(TmpCase):
    def test_write_file_bytes_exact(self) -> None:
        data = b"line1\r\nline2\n\x00binary\xff"
        wt.write_file(self.repo, "a/b/c.bin", "100644", data)
        self.assertEqual(self.read("a/b/c.bin"), data)
        wt.write_file(self.repo, "a/b/c.bin", "100644", b"new")
        self.assertEqual(self.read("a/b/c.bin"), b"new")

    @unittest.skipIf(IS_WINDOWS, "chmod modes are POSIX only")
    def test_write_file_mode(self) -> None:
        wt.write_file(self.repo, "x.sh", "100755", b"#!")
        self.assertTrue(os.stat(self.path("x.sh")).st_mode & 0o111)
        wt.write_file(self.repo, "x.sh", "100644", b"#!")
        self.assertFalse(os.stat(self.path("x.sh")).st_mode & 0o111)

    def test_write_file_replaces_things_in_the_way(self) -> None:
        self.write("p", b"file where a dir is needed")
        wt.write_file(self.repo, "p/x", "100644", b"x")
        self.assertEqual(self.read("p/x"), b"x")
        os.makedirs(self.path("q/empty"))
        wt.write_file(self.repo, "q", "100644", b"q")
        self.assertEqual(self.read("q"), b"q")

    def test_remove_file_prunes(self) -> None:
        self.write("a/b/c.txt", b"c")
        self.write("a/keep.txt", b"k")
        wt.remove_file(self.repo, "a/b/c.txt")
        self.assertFalse(os.path.exists(self.path("a/b")))
        self.assertTrue(os.path.exists(self.path("a/keep.txt")))
        wt.remove_file(self.repo, "a/keep.txt")
        self.assertFalse(os.path.exists(self.path("a")))
        self.assertTrue(os.path.isdir(self.root))
        wt.remove_file(self.repo, "never/existed.txt")  # missing is fine

    def test_remove_read_only_file(self) -> None:
        p = self.write("ro.txt", b"r")
        os.chmod(p, 0o444)
        wt.remove_file(self.repo, "ro.txt")
        self.assertFalse(os.path.exists(p))


class CheckoutTests(TmpCase):
    """checkout_tree between two flat trees, driving the real disk + an in-memory index."""

    def setUp(self) -> None:
        super().setUp()
        self.old = {"same.txt": self.blob(b"same\n"), "change.txt": self.blob(b"v1\n"),
                    "gone.txt": self.blob(b"bye\n"), "d/inner.txt": self.blob(b"in\n")}
        self.new = {"same.txt": self.blob(b"same\n"), "change.txt": self.blob(b"v2\n"),
                    "added.txt": self.blob(b"hi\n"), "d/inner.txt": self.blob(b"in\n")}
        self.index = Index()
        wt.checkout_tree(self.repo, self.index, {}, self.old)  # materialize old

    def snapshot(self) -> dict[str, bytes]:
        out = {}
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if d != ".minigit"]
            for fn in fns:
                full = os.path.join(dp, fn)
                with open(full, "rb") as f:
                    out[os.path.relpath(full, self.root).replace(os.sep, "/")] = f.read()
        return out

    def test_initial_checkout(self) -> None:
        self.assertEqual(self.snapshot(), {"same.txt": b"same\n", "change.txt": b"v1\n",
                                           "gone.txt": b"bye\n", "d/inner.txt": b"in\n"})
        self.assertEqual(self.index.flat(), self.old)
        for e in self.index.entries():
            self.assertEqual(e.size, os.stat(self.path(e.path)).st_size)

    def test_clean_switch(self) -> None:
        wt.checkout_tree(self.repo, self.index, self.old, self.new)
        self.assertEqual(self.snapshot(), {"same.txt": b"same\n", "change.txt": b"v2\n",
                                           "added.txt": b"hi\n", "d/inner.txt": b"in\n"})
        self.assertEqual(self.index.flat(), self.new)
        # and back
        wt.checkout_tree(self.repo, self.index, self.new, self.old)
        self.assertEqual(self.index.flat(), self.old)
        self.assertEqual(self.read("gone.txt"), b"bye\n")
        self.assertFalse(os.path.exists(self.path("added.txt")))

    def test_carries_non_conflicting_edits(self) -> None:
        self.write("same.txt", b"local edit\n")          # unstaged edit to an unchanged path
        self.write("new-local.txt", b"untracked\n")      # untracked file not in either tree
        self.write("staged.txt", b"staged\n")
        wt.stage_file(self.repo, self.index, "staged.txt")  # staged new file
        wt.checkout_tree(self.repo, self.index, self.old, self.new)
        self.assertEqual(self.read("same.txt"), b"local edit\n")
        self.assertEqual(self.read("new-local.txt"), b"untracked\n")
        self.assertIn("staged.txt", self.index.flat())
        self.assertEqual(self.index.flat()["same.txt"], self.old["same.txt"])
        self.assertEqual(self.read("change.txt"), b"v2\n")

    def test_refuses_conflicting_edit_and_touches_nothing(self) -> None:
        self.write("change.txt", b"my work\n")
        before, before_index = self.snapshot(), self.index.serialize()
        with self.assertRaises(errors.CheckoutConflictError) as cm:
            wt.checkout_tree(self.repo, self.index, self.old, self.new)
        self.assertEqual(cm.exception.paths, ["change.txt"])
        self.assertEqual(
            str(cm.exception.message),
            "Your local changes to the following files would be overwritten by checkout:\n"
            "\tchange.txt\n"
            "Please commit your changes or stash them before you switch branches.\n"
            "Aborting")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.index.serialize(), before_index)

    def test_refuses_deleting_modified_file(self) -> None:
        self.write("gone.txt", b"edited\n")
        with self.assertRaises(errors.CheckoutConflictError) as cm:
            wt.checkout_tree(self.repo, self.index, self.old, self.new)
        self.assertEqual(cm.exception.paths, ["gone.txt"])

    def test_refuses_staged_change_differing_from_both(self) -> None:
        self.write("change.txt", b"staged other\n")
        wt.stage_file(self.repo, self.index, "change.txt")
        with self.assertRaises(errors.CheckoutConflictError):
            wt.checkout_tree(self.repo, self.index, self.old, self.new)

    def test_staged_change_equal_to_target_is_fine(self) -> None:
        self.write("change.txt", b"v2\n")
        wt.stage_file(self.repo, self.index, "change.txt")
        wt.checkout_tree(self.repo, self.index, self.old, self.new)
        self.assertEqual(self.index.flat(), self.new)
        self.assertEqual(self.read("change.txt"), b"v2\n")

    def test_refuses_untracked_file_in_the_way(self) -> None:
        self.write("added.txt", b"my untracked\n")
        with self.assertRaises(errors.CheckoutConflictError) as cm:
            wt.checkout_tree(self.repo, self.index, self.old, self.new)
        self.assertEqual(cm.exception.paths, ["added.txt"])
        self.assertEqual(
            cm.exception.message,
            "The following untracked working tree files would be overwritten by checkout:\n"
            "\tadded.txt\n"
            "Please move or remove them before you switch branches.\n"
            "Aborting")
        self.assertEqual(self.read("added.txt"), b"my untracked\n")
        self.assertEqual(self.read("change.txt"), b"v1\n")

    def test_local_changes_and_untracked_files_are_reported_separately(self) -> None:
        self.write("change.txt", b"my work\n")
        self.write("added.txt", b"my untracked\n")
        with self.assertRaises(errors.CheckoutConflictError) as cm:
            wt.checkout_tree(self.repo, self.index, self.old, self.new, operation="rebase")
        self.assertEqual(cm.exception.paths, ["added.txt", "change.txt"])
        self.assertEqual(
            cm.exception.message,
            "Your local changes to the following files would be overwritten by rebase:\n"
            "\tchange.txt\n"
            "Please commit your changes or stash them before you rebase.\n"
            "error: The following untracked working tree files would be overwritten by rebase:\n"
            "\tadded.txt\n"
            "Please move or remove them before you rebase.\n"
            "Aborting")

    def test_untracked_identical_file_is_adopted(self) -> None:
        self.write("added.txt", b"hi\n")
        wt.checkout_tree(self.repo, self.index, self.old, self.new)
        self.assertEqual(self.index.flat()["added.txt"], self.new["added.txt"])

    def test_unmerged_path_conflicts(self) -> None:
        self.index.put(IndexEntry("change.txt", "100644", blob_sha(b"x"), 1, 0, 2))
        with self.assertRaises(errors.CheckoutConflictError) as cm:
            wt.checkout_tree(self.repo, self.index, self.old, self.new)
        self.assertIn("change.txt", cm.exception.paths)

    def test_dir_file_clash_untracked_in_dir(self) -> None:
        # target needs file 'd' while disk has dir d/ with an untracked file
        target = {"same.txt": self.old["same.txt"], "d": self.blob(b"now a file\n")}
        self.write("d/untracked.txt", b"precious\n")
        with self.assertRaises(errors.CheckoutConflictError) as cm:
            wt.checkout_tree(self.repo, self.index, self.old, target)
        self.assertIn("d", cm.exception.paths)
        self.assertEqual(self.read("d/untracked.txt"), b"precious\n")
        self.assertEqual(self.read("d/inner.txt"), b"in\n")

    def test_dir_to_file_transition_when_clean(self) -> None:
        target = {"same.txt": self.old["same.txt"], "d": self.blob(b"now a file\n")}
        wt.checkout_tree(self.repo, self.index, self.old, target)
        self.assertEqual(self.read("d"), b"now a file\n")
        self.assertEqual(self.index.flat(), target)
        wt.checkout_tree(self.repo, self.index, target, self.old)  # file -> dir again
        self.assertEqual(self.read("d/inner.txt"), b"in\n")
        self.assertEqual(self.index.flat(), self.old)

    def test_file_dir_clash_untracked_file(self) -> None:
        # target needs 'u/x' while disk has an untracked file 'u'
        target = dict(self.old)
        target["u/x"] = self.blob(b"x\n")
        self.write("u", b"untracked file\n")
        with self.assertRaises(errors.CheckoutConflictError) as cm:
            wt.checkout_tree(self.repo, self.index, self.old, target)
        self.assertEqual(cm.exception.paths, ["u"])
        self.assertEqual(self.read("u"), b"untracked file\n")

    def test_force_overwrites(self) -> None:
        self.write("change.txt", b"my work\n")
        self.write("added.txt", b"untracked\n")
        wt.checkout_tree(self.repo, self.index, self.old, self.new, force=True)
        self.assertEqual(self.read("change.txt"), b"v2\n")
        self.assertEqual(self.read("added.txt"), b"hi\n")
        self.assertEqual(self.index.flat(), self.new)

    def test_operation_name_in_message(self) -> None:
        self.write("change.txt", b"mine\n")
        with self.assertRaises(errors.CheckoutConflictError) as cm:
            wt.checkout_tree(self.repo, self.index, self.old, self.new, operation="merge")
        self.assertIn("would be overwritten by merge:\n\tchange.txt\n", cm.exception.message)

    def test_crlf_and_binary_preserved(self) -> None:
        target = {"w.txt": self.blob(b"a\r\nb\r\n"), "bin": self.blob(b"\x00\x01\xff")}
        wt.checkout_tree(self.repo, self.index, self.old, target)
        self.assertEqual(self.read("w.txt"), b"a\r\nb\r\n")
        self.assertEqual(self.read("bin"), b"\x00\x01\xff")
        self.assertEqual(wt.worktree_entry(self.repo, self.index, "w.txt"), target["w.txt"])

    @unittest.skipIf(IS_WINDOWS, "exec bit is POSIX only")
    def test_exec_mode_written(self) -> None:
        target = dict(self.old)
        target["run.sh"] = self.blob(b"#!/bin/sh\n", "100755")
        wt.checkout_tree(self.repo, self.index, self.old, target)
        self.assertTrue(os.stat(self.path("run.sh")).st_mode & 0o100)
        self.assertEqual(wt.worktree_entry(self.repo, self.index, "run.sh"), target["run.sh"])

    def test_ensure_unmodified(self) -> None:
        wt.ensure_unmodified(self.repo, self.index, {"same.txt": self.old["same.txt"], "nothere": None}, "merge")
        self.write("same.txt", b"dirty\n")
        self.write("nothere", b"surprise\n")
        with self.assertRaises(errors.CheckoutConflictError) as cm:
            wt.ensure_unmodified(self.repo, self.index,
                                 {"same.txt": self.old["same.txt"], "nothere": None,
                                  "change.txt": self.old["change.txt"]}, "merge")
        self.assertEqual(cm.exception.paths, ["nothere", "same.txt"])
        self.assertIn("Your local changes to the following files would be overwritten by merge:\n\tsame.txt\n",
                      cm.exception.message)
        self.assertIn("untracked working tree files would be overwritten by merge:\n\tnothere\n",
                      cm.exception.message)


class RealRepositoryTests(unittest.TestCase):
    """Same flows against M1's real Repository, when it exists."""

    def setUp(self) -> None:
        try:
            from minigit.repo import Repository
            Repository.init  # noqa: B018
        except (ImportError, AttributeError):
            self.skipTest("minigit.repo.Repository not available yet")
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.realpath(self._tmp.name)
        self.repo, _ = Repository.init(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stage_and_checkout_round_trip(self) -> None:
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.root, "sub", "x"), "wb") as f:
            f.write(b"hello\n")
        with locked_index(self.repo) as ix:
            e = wt.stage_file(self.repo, ix, "sub\\x")
        self.assertEqual(e.sha, "ce013625030ba8dba906f756967f9e9ca394464a")
        self.assertEqual(self.repo.objects.read_typed(e.sha, "blob"), b"hello\n")
        flat = read_index(self.repo).flat()
        with locked_index(self.repo) as ix:
            wt.checkout_tree(self.repo, ix, flat, {})
        self.assertFalse(os.path.exists(os.path.join(self.root, "sub")))
        with locked_index(self.repo) as ix:
            wt.checkout_tree(self.repo, ix, {}, flat)
        self.assertEqual(read_index(self.repo).flat(), flat)
        files = wt.scan_files(self.repo, IgnoreMatcher(self.root))
        self.assertEqual(list(files), ["sub/x"])


# ----------------------------------------------------------------------------- case-insensitive / Windows names
def _probe_ci(directory: str) -> bool:
    return wt._probe_case_insensitive(directory)


class FilesystemQuirkHelperTests(TmpCase):
    """Pure helpers: the same answers on every platform."""

    def test_windows_name_problem(self) -> None:
        from minigit.util import windows_name_problem
        for bad in ("NUL", "nul", "con.txt", "COM1", "lpt9.log", "aux ", "trail.", "trail ", "what?.txt",
                    'q"uote', "pi|pe", "lt<gt>", "a*b", "tab\there"):
            self.assertIsNotNone(windows_name_problem(bad), bad)
        for good in ("a.txt", "null", "console", "com10", "nul_", ".hidden", "README", "x.y.z"):
            self.assertIsNone(windows_name_problem(good), good)

    def test_case_collisions(self) -> None:
        self.assertEqual(wt.case_collisions(["README.txt", "readme.txt", "x"]), [["README.txt", "readme.txt"]])
        # a directory clash is reported once, at the directory
        self.assertEqual(wt.case_collisions(["Sub/a", "sub/b", "sub/c"]), [["Sub", "sub"]])
        self.assertEqual(wt.case_collisions(["readme", "README/x"]), [["README", "readme"]])
        self.assertEqual(wt.case_collisions(["a/b", "a/c", "B"]), [])

    def test_probe_and_config_override(self) -> None:
        wt._CASE_CACHE.clear()
        self.assertIsInstance(wt.fs_ignores_case(self.repo), bool)
        self.assertEqual(os.listdir(self.repo.gitdir), ["objects"], "the probe file was left behind")

        class Cfg:
            def __init__(self, value):
                self.value = value

            def get(self, key):
                return self.value if key == "core.ignorecase" else None

        for raw, expected in (("true", True), ("false", False)):
            wt._CASE_CACHE.clear()
            self.repo.config = Cfg(raw)
            self.assertIs(wt.fs_ignores_case(self.repo), expected)
        wt._CASE_CACHE.clear()

    def test_index_case_lookups(self) -> None:
        ix = Index([IndexEntry("Sub/Readme.md", "100644", blob_sha(b"x"))])
        self.assertEqual(ix.case_variant("sub/README.md"), "Sub/Readme.md")
        self.assertIsNone(ix.case_variant("Sub/Readme.md"))
        self.assertEqual(ix.tracked_spelling("SUB/README.MD"), "Sub/Readme.md")
        self.assertEqual(ix.tracked_spelling("sub/new.txt"), "Sub/new.txt")
        self.assertEqual(ix.tracked_spelling("other/x"), "other/x")
        ix.put(IndexEntry("readme.md", "100644", blob_sha(b"y")))
        self.assertEqual(ix.case_variant("README.MD"), "readme.md")
        ix.remove("readme.md")
        self.assertIsNone(ix.case_variant("README.MD"))


class CaseInsensitiveWorktreeTests(TmpCase):
    """Behaviour on a case-insensitive worktree. ``fs_ignores_case`` is patched, so these run on
    case-sensitive filesystems too; each test only relies on what both kinds of filesystem do."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(wt, "fs_ignores_case", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def snapshot(self) -> dict[str, bytes]:
        return CheckoutTests.snapshot(self)  # type: ignore[arg-type]

    def test_worktree_entry_ignores_a_tracked_case_variant(self) -> None:
        self.write("Readme.md", b"x\n")
        ix = Index()
        wt.stage_file(self.repo, ix, "Readme.md")
        self.assertIsNone(wt.worktree_entry(self.repo, ix, "README.md"))
        self.assertEqual(wt.worktree_entry(self.repo, ix, "Readme.md"), ("100644", blob_sha(b"x\n")))

    def test_stage_file_uses_tracked_spelling(self) -> None:
        self.write("sub/c.txt", b"v1\n")
        ix = Index()
        wt.stage_file(self.repo, ix, "sub/c.txt")
        if _probe_ci(self.root):
            self.write("sub/c.txt", b"v2\n")
            e = wt.stage_file(self.repo, ix, "SUB/C.TXT")
            self.assertEqual(e.path, "sub/c.txt")
            self.assertEqual([x.path for x in ix.entries()], ["sub/c.txt"])
            self.assertEqual(ix.get("sub/c.txt").sha, blob_sha(b"v2\n"))
        # a new file under a tracked directory takes the directory's tracked spelling
        self.assertEqual(wt.canonical_path(self.repo, ix, "SUB/new.txt"), "sub/new.txt")

    def test_canonical_path_takes_disk_spelling_for_new_paths(self) -> None:
        self.write("Docs/Guide.md", b"g\n")
        self.assertEqual(wt.canonical_path(self.repo, Index(), "docs/guide.md"), "Docs/Guide.md")
        self.assertEqual(wt.canonical_path(self.repo, Index(), "docs/missing.md"), "Docs/missing.md")

    def test_case_only_rename_switch_keeps_the_file(self) -> None:
        upper = {"README.md": self.blob(b"hi\n"), "o.txt": self.blob(b"o\n")}
        lower = {"readme.md": self.blob(b"hi\n"), "o.txt": self.blob(b"o\n")}   # same content
        ix = Index()
        wt.checkout_tree(self.repo, ix, {}, upper)
        wt.checkout_tree(self.repo, ix, upper, lower)
        self.assertEqual(self.snapshot(), {"readme.md": b"hi\n", "o.txt": b"o\n"})
        self.assertEqual(ix.flat(), lower)
        lower2 = {"readme.md": self.blob(b"lower\n"), "o.txt": self.blob(b"o\n")}
        wt.checkout_tree(self.repo, ix, lower, lower2)
        wt.checkout_tree(self.repo, ix, lower2, upper)
        self.assertEqual(self.snapshot(), {"README.md": b"hi\n", "o.txt": b"o\n"})
        self.assertEqual(ix.flat(), upper)
        self.assertEqual(wt.worktree_entry(self.repo, ix, "README.md"), upper["README.md"])

    def test_case_variant_staged_file_is_not_overwritten(self) -> None:
        self.write("README.md", b"staged work\n")
        ix = Index()
        wt.stage_file(self.repo, ix, "README.md")
        with self.assertRaises(errors.CheckoutConflictError) as cm:
            wt.checkout_tree(self.repo, ix, {}, {"readme.md": self.blob(b"theirs\n")})
        self.assertEqual(cm.exception.paths, ["README.md"])
        self.assertEqual(self.read("README.md"), b"staged work\n")

    def test_colliding_tree_is_refused_before_anything_is_written(self) -> None:
        base = {"base.txt": self.blob(b"base\n")}
        ix = Index()
        wt.checkout_tree(self.repo, ix, {}, base)
        target = {"base.txt": self.blob(b"changed\n"), "README.txt": self.blob(b"a\n"),
                  "readme.txt": self.blob(b"b\n")}
        for force in (False, True):
            with self.assertRaises(errors.MinigitError) as cm:
                wt.checkout_tree(self.repo, ix, base, target, force=force)
            self.assertIn("'README.txt' and 'readme.txt'", str(cm.exception))
            self.assertEqual(self.snapshot(), {"base.txt": b"base\n"})
            self.assertEqual(ix.flat(), base)
        with self.assertRaises(errors.MinigitError):
            wt.checkout_tree(self.repo, ix, base, {"Sub/a": self.blob(b"a\n"), "sub/b": self.blob(b"b\n")})

    def test_status_does_not_list_a_case_renamed_tracked_file(self) -> None:
        from minigit.repo import Repository
        from minigit.status import compute_status
        repo, _ = Repository.init(os.path.join(self.root, "r"))
        with open(os.path.join(repo.worktree, "Readme.md"), "wb") as f:
            f.write(b"x\n")
        with locked_index(repo) as ix:
            wt.stage_file(repo, ix, "Readme.md")
        os.rename(os.path.join(repo.worktree, "Readme.md"), os.path.join(repo.worktree, "tmp"))
        os.rename(os.path.join(repo.worktree, "tmp"), os.path.join(repo.worktree, "README.md"))
        entries = compute_status(repo).entries
        self.assertNotIn(("??", "README.md"), entries)
        if _probe_ci(self.root):
            self.assertEqual(entries, [("A ", "Readme.md")])


class WindowsNameTests(TmpCase):
    """``fs_is_windows`` is patched: the refusal must happen before anything is written."""

    def test_unstorable_names_refused_before_writing(self) -> None:
        base = {"base.txt": self.blob(b"base\n")}
        ix = Index()
        wt.checkout_tree(self.repo, ix, {}, base)
        with mock.patch.object(wt, "fs_is_windows", return_value=True):
            for name in ("NUL", "trail.", "trail ", "what?.txt", 'q"uote.txt', "dir./x", "con/y"):
                target = {"base.txt": self.blob(b"upstream\n"), name: self.blob(b"odd\n")}
                with self.assertRaises(errors.MinigitError) as cm:
                    wt.checkout_tree(self.repo, ix, base, target)
                self.assertIn(f"'{name}'", str(cm.exception))
                self.assertEqual(self.read("base.txt"), b"base\n")
                self.assertEqual(ix.flat(), base)
            # an unchanged path is not re-checked, and ordinary names pass
            wt.checkout_tree(self.repo, ix, base, {"base.txt": self.blob(b"v2\n"), "ok.txt": self.blob(b"ok\n")})
        self.assertEqual(self.read("ok.txt"), b"ok\n")

    def test_names_are_allowed_off_windows(self) -> None:
        with mock.patch.object(wt, "fs_is_windows", return_value=False):
            wt.check_checkout_paths(self.repo, {"what?.txt": self.blob(b"x")}, ["what?.txt"])


class WindowsRefAndRepoNameTests(unittest.TestCase):
    def test_reserved_ref_names_refused_on_windows(self) -> None:
        from minigit import refs
        with self.assertRaises(errors.MinigitError):
            refs._check_windows_ref_name("refs/heads/nul")
        with self.assertRaises(errors.MinigitError):
            refs._check_windows_ref_name("refs/heads/feat./x")
        refs._check_windows_ref_name("refs/heads/null")

    def test_default_branch_protection_sees_case_variants(self) -> None:
        from minigit.repo import Repository
        from minigit.server import _same_ref
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            repo, _ = Repository.init(td, bare=True, initial_branch="dup")
            repo.refs.update("refs/heads/dup", "1" * 40)
            self.assertTrue(_same_ref(repo, "refs/heads/dup", "refs/heads/dup"))
            self.assertEqual(_same_ref(repo, "refs/heads/DUP", "refs/heads/dup"), _probe_ci(td))
            self.assertFalse(_same_ref(repo, "refs/heads/other", "refs/heads/dup"))

    def test_repo_dir_escapes_windows_aliases(self) -> None:
        from minigit.auth import _repo_dir_name
        self.assertEqual(_repo_dir_name("foo"), "foo")
        self.assertEqual(_repo_dir_name("foo."), "foo%2E")
        self.assertEqual(_repo_dir_name("NUL"), "%4EUL")
        self.assertEqual(_repo_dir_name("con.git"), "%63on.git")
        self.assertEqual(len({_repo_dir_name(n).casefold() for n in ("foo", "foo.", "NUL", "nul.x")}), 4)


if __name__ == "__main__":
    unittest.main()

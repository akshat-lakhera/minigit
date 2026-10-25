"""tests.test_core -- owner: M1 core.

Unit tests for errors, util, objects, trees, config, refs, repo, graph and
revparse. Uses only the standard library and temp directories; it does not
import tests/helpers.py or any other module.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import zlib

from minigit import __version__
from minigit import graph, revparse
from minigit.config import Config, split_key
from minigit.errors import (
    CheckoutConflictError,
    LockError,
    MinigitError,
    NotARepositoryError,
    ObjectError,
    PushRejectedError,
    RefUpdateError,
    RevisionError,
    UsageError,
)
from minigit.objects import (
    EMPTY_TREE,
    Commit,
    ObjectStore,
    Signature,
    TreeEntry,
    decode_object,
    encode_object,
    hash_object,
    parse_commit,
    parse_tree,
    serialize_commit,
    serialize_tree,
)
from minigit.refs import ANY, RefStore, check_ref_name, is_hex_sha, short_ref
from minigit.repo import Repository
from minigit.trees import build_tree, commit_flat, commit_tree_sha, flatten_tree
from minigit.util import (
    LockFile,
    atomic_write,
    format_date,
    format_tz,
    is_binary,
    now_with_tz,
    parse_tz,
    replace_with_retry,
    sha1_hex,
    split_lines,
    unlink_with_retry,
    validate_name,
    validate_rel_path,
)

EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
HELLO_BLOB = "ce013625030ba8dba906f756967f9e9ca394464a"


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._td.name
        self._env_backup = {k: os.environ.get(k) for k in (
            "MINIGIT_GLOBAL_CONFIG", "MINIGIT_AUTHOR_NAME", "MINIGIT_AUTHOR_EMAIL", "MINIGIT_AUTHOR_DATE")}
        os.environ["MINIGIT_GLOBAL_CONFIG"] = os.path.join(self.tmp, "globalconfig")
        for k in ("MINIGIT_AUTHOR_NAME", "MINIGIT_AUTHOR_EMAIL", "MINIGIT_AUTHOR_DATE"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()

    def p(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)


def make_commit(store: ObjectStore, tree: str, parents, ts: int, msg: str = "m", tz: int = 0) -> str:
    sig = Signature("Tester", "t@example.com", ts, tz)
    return store.write("commit", serialize_commit(Commit(tree, tuple(parents), sig, sig, msg + "\n")))


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class ErrorsTest(unittest.TestCase):
    def test_version(self):
        self.assertEqual(__version__, "0.1.0")

    def test_hierarchy_and_attrs(self):
        e = MinigitError("boom", hint="try again")
        self.assertEqual(str(e), "boom")
        self.assertEqual(e.message, "boom")
        self.assertEqual(e.hint, "try again")
        self.assertEqual(e.exit_code, 1)
        self.assertEqual(UsageError("u").exit_code, 2)
        self.assertIsInstance(NotARepositoryError("x"), MinigitError)
        oe = ObjectError("bad", sha="ab" * 20)
        self.assertEqual(oe.sha, "ab" * 20)
        r = RefUpdateError("c", ref="refs/heads/main", expected=None, actual="x")
        self.assertEqual((r.ref, r.expected, r.actual), ("refs/heads/main", None, "x"))
        c = CheckoutConflictError("m", paths=["a", "b"])
        self.assertEqual(c.paths, ["a", "b"])
        pr = PushRejectedError("rej", results=[{"ref": "r"}], url="http://x", status=409, code="rejected")
        self.assertEqual(pr.url, "http://x")
        self.assertEqual(pr.status, 409)
        self.assertEqual(pr.code, "rejected")
        self.assertEqual(pr.results, [{"ref": "r"}])
        self.assertIsInstance(pr, MinigitError)


# ---------------------------------------------------------------------------
# util
# ---------------------------------------------------------------------------


class UtilTest(TempDirCase):
    def test_sha1_hex(self):
        self.assertEqual(sha1_hex(b"blob 0\0"), EMPTY_BLOB)

    def test_split_lines(self):
        self.assertEqual(split_lines(b""), [])
        self.assertEqual(split_lines(b"a\nb\n"), [b"a\n", b"b\n"])
        self.assertEqual(split_lines(b"a\nb"), [b"a\n", b"b"])
        self.assertEqual(split_lines(b"a\r\nb\rc\n"), [b"a\r\n", b"b\rc\n"])
        self.assertEqual(split_lines(b"\n\n"), [b"\n", b"\n"])

    def test_is_binary(self):
        self.assertFalse(is_binary(b"hello\n"))
        self.assertTrue(is_binary(b"he\0llo"))
        self.assertFalse(is_binary(b"x" * 8000 + b"\0"))

    def test_tz_round_trip(self):
        self.assertEqual(format_tz(19800), "+0530")
        self.assertEqual(format_tz(-25200), "-0700")
        self.assertEqual(format_tz(0), "+0000")
        self.assertEqual(parse_tz("+0530"), 19800)
        self.assertEqual(parse_tz("-0700"), -25200)
        self.assertEqual(parse_tz("+0000"), 0)
        with self.assertRaises(MinigitError):
            parse_tz("0530")
        ts, off = now_with_tz()
        self.assertIsInstance(ts, int)
        self.assertEqual(parse_tz(format_tz(off)), off)

    def test_format_date(self):
        self.assertEqual(format_date(1792467303, 19800), "Tue Oct 20 09:05:03 2026 +0530")
        self.assertEqual(format_date(0, 0), "Thu Jan 1 00:00:00 1970 +0000")
        self.assertEqual(format_date(1112911993, -25200), "Thu Apr 7 15:13:13 2005 -0700")

    def test_validate_name(self):
        for ok in ("a", "a.txt", ".hidden", "with space", "ünï", ".minigitignore", "minigit", "x.minigit"):
            validate_name(ok)
        for bad in ("", ".", "..", ".minigit", ".MiniGit", "a/b", "a\\b", "a\0b",
                    # Windows aliases of the gitdir, drive letters and NTFS streams
                    ".minigit.", ".minigit ", ".MINIGIT. .", "MINIGI~1", "minigi~12", "C:", "a:b"):
            with self.assertRaises(ObjectError, msg=repr(bad)):
                validate_name(bad)

    def test_validate_rel_path(self):
        validate_rel_path("a")
        validate_rel_path("a/b/c.txt")
        for bad in ("", "/a", "C:/a", "a//b", "a/", "a/../b", "./a", ".minigit/x", "a\\b"):
            with self.assertRaises(MinigitError, msg=repr(bad)):
                validate_rel_path(bad)

    def test_atomic_write_and_replace(self):
        path = self.p("sub", "dir", "f.txt")
        atomic_write(path, b"one\r\n")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"one\r\n")
        atomic_write(path, b"two")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"two")
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if ".tmp-" in n], [])
        src = self.p("src")
        atomic_write(src, b"s")
        replace_with_retry(src, path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"s")
        unlink_with_retry(path)
        unlink_with_retry(path)  # missing is fine
        self.assertFalse(os.path.exists(path))

    def test_lockfile_commit_and_rollback(self):
        path = self.p("refs", "heads", "main")
        with LockFile(path) as lock:
            self.assertTrue(os.path.exists(path + ".lock"))
            lock.write(b"abc\n")
            lock.commit()
        self.assertFalse(os.path.exists(path + ".lock"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"abc\n")
        # no commit -> lock removed, file untouched
        with LockFile(path) as lock:
            lock.write(b"zzz")
        self.assertFalse(os.path.exists(path + ".lock"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"abc\n")
        # exception inside -> lock removed
        with self.assertRaises(RuntimeError):
            with LockFile(path):
                raise RuntimeError("x")
        self.assertFalse(os.path.exists(path + ".lock"))

    def test_lockfile_held_raises(self):
        path = self.p("index")
        with LockFile(path):
            with self.assertRaises(LockError) as cm:
                LockFile(path).acquire()
            self.assertIn("index.lock exists", cm.exception.message)
            self.assertIn("unable to lock", cm.exception.message)
            t0 = time.monotonic()
            with self.assertRaises(LockError):
                LockFile(path, timeout=0.2).acquire()
            self.assertGreaterEqual(time.monotonic() - t0, 0.15)

    def test_lockfile_timeout_waits_for_release(self):
        path = self.p("waited")
        first = LockFile(path).acquire()

        def release():
            time.sleep(0.15)
            first.rollback()

        threading.Thread(target=release).start()
        with LockFile(path, timeout=3.0) as lock:
            lock.write(b"ok")
            lock.commit()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"ok")


# ---------------------------------------------------------------------------
# objects
# ---------------------------------------------------------------------------


class ObjectsTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = ObjectStore(self.p("objects"))

    def test_hash_fixtures(self):
        self.assertEqual(hash_object("blob", b""), EMPTY_BLOB)
        self.assertEqual(hash_object("blob", b"hello\n"), HELLO_BLOB)
        self.assertEqual(hash_object("tree", b""), EMPTY_TREE)
        self.assertEqual(hash_object("tree", serialize_tree([])), EMPTY_TREE)

    def test_encode_decode(self):
        raw = encode_object("blob", b"hello\n")
        self.assertEqual(raw, b"blob 6\0hello\n")
        self.assertEqual(decode_object(raw), ("blob", b"hello\n"))
        for bad in (b"blob 7\0hello\n", b"blob\0x", b"nope 1\0x", b"blob 01\0x", b"blob x\0", b"xx"):
            with self.assertRaises(ObjectError, msg=repr(bad)):
                decode_object(bad)
        with self.assertRaises(ObjectError):
            encode_object("tag", b"")

    def test_store_round_trip(self):
        sha = self.store.write("blob", b"hello\n")
        self.assertEqual(sha, HELLO_BLOB)
        self.assertTrue(self.store.has(sha))
        self.assertFalse(self.store.has("0" * 40))
        self.assertFalse(self.store.has("zz"))
        self.assertEqual(self.store.read(sha), ("blob", b"hello\n"))
        self.assertEqual(self.store.read_typed(sha, "blob"), b"hello\n")
        with self.assertRaises(ObjectError):
            self.store.read_typed(sha, "commit")
        self.assertEqual(zlib.decompress(self.store.read_raw(sha)), b"blob 6\0hello\n")
        self.assertEqual(os.path.relpath(self.store.path(sha), self.store.dir).replace("\\", "/"),
                         sha[:2] + "/" + sha[2:])
        # idempotent write
        self.assertEqual(self.store.write("blob", b"hello\n"), sha)
        self.assertEqual([n for n in os.listdir(os.path.join(self.store.dir, sha[:2])) if n.startswith("tmp-")], [])
        with self.assertRaises(ObjectError) as cm:
            self.store.read("0" * 40)
        self.assertIn("not found", cm.exception.message)

    def test_store_corruption(self):
        sha = self.store.write("blob", b"data\n")
        path = self.store.path(sha)
        with open(path, "wb") as f:
            f.write(zlib.compress(b"blob 5\0datb\n"))
        with self.assertRaises(ObjectError):
            self.store.read(sha)
        with open(path, "wb") as f:
            f.write(b"not zlib")
        with self.assertRaises(ObjectError):
            self.store.read(sha)
        with open(path, "wb") as f:
            f.write(zlib.compress(b"blob 5\0data\n")[:-4])
        with self.assertRaises(ObjectError):
            self.store.read(sha)
        with open(path, "wb") as f:
            f.write(zlib.compress(b"blob 5\0data\n") + b"trailing")
        with self.assertRaises(ObjectError):
            self.store.read(sha)

    def test_resolve_prefix_and_iter_all(self):
        a = self.store.write("blob", b"a")
        b = self.store.write("blob", b"b")
        self.assertEqual(self.store.resolve_prefix(a[:6]), [a])
        self.assertEqual(self.store.resolve_prefix(a), [a])
        self.assertEqual(self.store.resolve_prefix(a[:3]), [])
        self.assertEqual(self.store.resolve_prefix("zzzz"), [])
        self.assertEqual(self.store.resolve_prefix("0" * 40), [])
        self.assertEqual(sorted(self.store.iter_all()), sorted([a, b]))
        self.assertEqual(list(ObjectStore(self.p("missing")).iter_all()), [])

    def test_tree_sort_order(self):
        sha = "1" * 40
        entries = [
            TreeEntry("100644", "a0", sha),
            TreeEntry("40000", "a", sha),
            TreeEntry("100644", "a.txt", sha),
            TreeEntry("100755", "a-b", sha),
        ]
        data = serialize_tree(entries)
        names = [e.name for e in parse_tree(data)]
        self.assertEqual(names, ["a-b", "a.txt", "a", "a0"])
        self.assertEqual(parse_tree(data), sorted(entries, key=lambda e: names.index(e.name)))
        self.assertEqual(serialize_tree(parse_tree(data)), data)
        self.assertEqual(parse_tree(b""), [])
        self.assertEqual(data[:8], b"100755 a")
        self.assertIn(b"\0" + bytes.fromhex(sha), data)

    def test_tree_bad_names_and_modes(self):
        sha = "1" * 40
        for bad in ("", ".", "..", ".minigit", ".MINIGIT", "a/b", "a\\b", "a\0b"):
            with self.assertRaises(ObjectError, msg=repr(bad)):
                serialize_tree([TreeEntry("100644", bad, sha)])
            with self.assertRaises(ObjectError, msg=repr(bad)):
                parse_tree(b"100644 " + bad.encode() + b"\0" + bytes.fromhex(sha))
        with self.assertRaises(ObjectError):
            serialize_tree([TreeEntry("120000", "link", sha)])
        with self.assertRaises(ObjectError):
            parse_tree(b"040000 d\0" + bytes.fromhex(sha))
        with self.assertRaises(ObjectError):
            serialize_tree([TreeEntry("100644", "x", sha), TreeEntry("100755", "x", sha)])
        with self.assertRaises(ObjectError):
            parse_tree(b"100644 x\0" + b"\1" * 10)
        with self.assertRaises(ObjectError):
            serialize_tree([TreeEntry("100644", "x", "nothex")])

    def test_signature(self):
        s = Signature("Ann Lee", "ann@example.com", 1700000000, 19800)
        self.assertEqual(s.format(), "Ann Lee <ann@example.com> 1700000000 +0530")
        self.assertEqual(Signature.parse(s.format()), s)
        self.assertEqual(Signature.parse("X <y@z> 5 -0700"), Signature("X", "y@z", 5, -25200))
        self.assertEqual(Signature.parse("<y@z> 5 +0000").name, "")
        self.assertEqual(Signature("A <b>", "<c>", 1, 0).format(), "A b <c> 1 +0000")
        with self.assertRaises(ObjectError):
            Signature.parse("garbage")

    def test_commit_round_trip(self):
        tree = EMPTY_TREE
        author = Signature("A", "a@x", 1700000000, 19800)
        committer = Signature("C", "c@x", 1700000100, -3600)
        c = Commit(tree, ("a" * 40, "b" * 40), author, committer, "Subject line\n\nBody text\n")
        data = serialize_commit(c)
        self.assertEqual(
            data,
            b"tree " + tree.encode() + b"\n"
            b"parent " + b"a" * 40 + b"\n"
            b"parent " + b"b" * 40 + b"\n"
            b"author A <a@x> 1700000000 +0530\n"
            b"committer C <c@x> 1700000100 -0100\n"
            b"\n"
            b"Subject line\n\nBody text\n",
        )
        back = parse_commit(data)
        self.assertEqual(back, c)
        self.assertEqual(back.subject, "Subject line")
        self.assertEqual(serialize_commit(back), data)
        root = Commit(tree, (), author, author, "root")
        self.assertTrue(serialize_commit(root).endswith(b"\n\nroot\n"))
        self.assertEqual(parse_commit(serialize_commit(root)).parents, ())
        sha = self.store.write("commit", data)
        self.assertEqual(parse_commit(self.store.read_typed(sha, "commit")), c)
        with self.assertRaises(ObjectError):
            parse_commit(b"author A <a@x> 1 +0000\n\nmsg\n")
        with self.assertRaises(ObjectError):
            parse_commit(b"tree " + tree.encode() + b"\n\nmsg\n")


# ---------------------------------------------------------------------------
# trees
# ---------------------------------------------------------------------------


class TreesTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = ObjectStore(self.p("objects"))

    def test_build_and_flatten(self):
        blob = self.store.write("blob", b"x\n")
        flat = {"a.txt": ("100644", blob), "dir/b.txt": ("100755", blob), "dir/sub/c": ("100644", blob), "z": ("100644", blob)}
        root = build_tree(self.store, flat)
        self.assertEqual(flatten_tree(self.store, root), flat)
        entries = parse_tree(self.store.read_typed(root, "tree"))
        self.assertEqual([(e.mode, e.name) for e in entries], [("100644", "a.txt"), ("40000", "dir"), ("100644", "z")])
        self.assertEqual(build_tree(self.store, dict(flat)), root)  # deterministic
        self.assertEqual(build_tree(self.store, {}), EMPTY_TREE)
        self.assertTrue(self.store.has(EMPTY_TREE))
        self.assertEqual(flatten_tree(self.store, None), {})
        self.assertEqual(flatten_tree(self.store, EMPTY_TREE), {})

    def test_build_rejects_clash_and_bad_paths(self):
        blob = self.store.write("blob", b"x\n")
        with self.assertRaises(ObjectError):
            build_tree(self.store, {"a": ("100644", blob), "a/b": ("100644", blob)})
        with self.assertRaises(ObjectError):
            build_tree(self.store, {"a/..": ("100644", blob)})
        with self.assertRaises(ObjectError):
            build_tree(self.store, {"a": ("40000", blob)})
        with self.assertRaises(ObjectError):
            build_tree(self.store, {".minigit/x": ("100644", blob)})

    def test_commit_flat(self):
        blob = self.store.write("blob", b"x\n")
        tree = build_tree(self.store, {"f": ("100644", blob)})
        c = make_commit(self.store, tree, [], 1)
        self.assertEqual(commit_tree_sha(self.store, c), tree)
        self.assertEqual(commit_flat(self.store, c), {"f": ("100644", blob)})
        self.assertEqual(commit_flat(self.store, None), {})
        with self.assertRaises(ObjectError):
            commit_tree_sha(self.store, blob)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class ConfigTest(TempDirCase):
    def test_split_key(self):
        self.assertEqual(split_key("user.name"), ("user", None, "name"))
        self.assertEqual(split_key("remote.origin.url"), ("remote", "origin", "url"))
        self.assertEqual(split_key("branch.my.feature.merge"), ("branch", "my.feature", "merge"))
        self.assertEqual(split_key("User.Name"), ("user", None, "name"))
        for bad in ("nodot", "", ".x", "x."):
            with self.assertRaises(MinigitError, msg=bad):
                split_key(bad)

    def test_round_trip_and_format(self):
        local, glob = self.p("config"), self.p("gconfig")
        cfg = Config(local, glob)
        self.assertIsNone(cfg.get("user.name"))
        self.assertEqual(cfg.get("user.name", "d"), "d")
        cfg.set("core.bare", "false")
        cfg.set("remote.origin.url", "http://127.0.0.1:1/proj")
        cfg.set("User.Name", "Ann")
        cfg.set("branch.main.remote", "origin")
        cfg.set("branch.main.merge", "refs/heads/main")
        with open(local, "rb") as f:
            text = f.read()
        self.assertEqual(
            text,
            b"[core]\n\tbare = false\n"
            b'[remote "origin"]\n\turl = http://127.0.0.1:1/proj\n'
            b"[user]\n\tname = Ann\n"
            b'[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n',
        )
        cfg2 = Config(local, glob)
        self.assertEqual(cfg2.get("user.name"), "Ann")
        self.assertEqual(cfg2.get("USER.NAME"), "Ann")
        self.assertEqual(cfg2.get("remote.origin.url"), "http://127.0.0.1:1/proj")
        self.assertFalse(cfg2.get_bool("core.bare"))
        self.assertTrue(cfg2.get_bool("missing.key", True))
        self.assertEqual(cfg2.subsections("remote"), ["origin"])
        self.assertEqual(cfg2.subsections("branch"), ["main"])
        self.assertEqual(cfg2.subsections("nothing"), [])
        self.assertEqual(
            cfg2.items("local"),
            [("core.bare", "false"), ("remote.origin.url", "http://127.0.0.1:1/proj"), ("user.name", "Ann"),
             ("branch.main.remote", "origin"), ("branch.main.merge", "refs/heads/main")],
        )
        # overwrite keeps a single entry
        cfg2.set("user.name", "Bob")
        self.assertEqual([v for k, v in Config(local, glob).items("local") if k == "user.name"], ["Bob"])
        self.assertTrue(cfg2.unset("user.name"))
        self.assertFalse(cfg2.unset("user.name"))
        self.assertIsNone(Config(local, glob).get("user.name"))
        cfg2.remove_section("remote", "origin")
        self.assertIsNone(Config(local, glob).get("remote.origin.url"))
        self.assertEqual(Config(local, glob).subsections("remote"), [])
        self.assertEqual(Config(local, glob).get("branch.main.merge"), "refs/heads/main")

    def test_global_fallback_and_scopes(self):
        local, glob = self.p("config"), self.p("gconfig")
        cfg = Config(local, glob)
        cfg.set("user.name", "Global", scope="global")
        cfg.set("user.email", "g@x", scope="global")
        self.assertTrue(os.path.exists(glob))
        self.assertFalse(os.path.exists(local))
        self.assertEqual(cfg.get("user.name"), "Global")
        cfg.set("user.name", "Local")
        self.assertEqual(cfg.get("user.name"), "Local")
        self.assertEqual(cfg.get("user.email"), "g@x")
        merged = dict(cfg.items())
        self.assertEqual(merged, {"user.name": "Local", "user.email": "g@x"})
        self.assertEqual(dict(cfg.items("global")), {"user.name": "Global", "user.email": "g@x"})
        # a fresh Config reads both files
        fresh = Config(local, glob)
        self.assertEqual(fresh.get("user.name"), "Local")
        # no local path -> local writes fail, global still works
        nolocal = Config(None, glob)
        self.assertEqual(nolocal.get("user.email"), "g@x")
        with self.assertRaises(MinigitError):
            nolocal.set("user.name", "x")
        # env-driven default global path
        os.environ["MINIGIT_GLOBAL_CONFIG"] = glob
        self.assertEqual(Config(None).get("user.email"), "g@x")

    def test_parse_comments_and_quotes(self):
        local = self.p("config")
        with open(local, "wb") as f:
            f.write(
                b"# comment\n; another\n[Core]\n\tBare = TRUE\n  filemode = false \n"
                b'[remote "ori\\"gin"]\n\turl = http://h/p  \n[section]\n\tflag\n'
            )
        cfg = Config(local, self.p("gconfig"))
        self.assertTrue(cfg.get_bool("core.bare"))
        self.assertEqual(cfg.get("core.filemode"), "false")
        self.assertEqual(cfg.subsections("remote"), ['ori"gin'])
        self.assertEqual(cfg.get('remote.ori"gin.url'), "http://h/p")
        self.assertEqual(cfg.get("section.flag"), "true")
        self.assertFalse(cfg.get_bool("core.filemode"))
        cfg.set("core.x", "maybe")
        with self.assertRaises(MinigitError):
            cfg.get_bool("core.x")


# ---------------------------------------------------------------------------
# refs
# ---------------------------------------------------------------------------


class RefNameTest(unittest.TestCase):
    def test_good_names(self):
        for name in ("HEAD", "refs/heads/main", "refs/heads/feature/x-y_z.1", "refs/remotes/origin/main", "refs/tags/v1"):
            check_ref_name(name)

    def test_bad_names(self):
        bad = [
            "", "main", "refs", "refs/", "refs/heads/", "refs/heads//x", "refs//heads/x", "refs/heads/.x",
            "refs/heads/x.lock", "refs/heads/a..b", "refs/heads/a@{1}", "refs/heads/a b", "refs/heads/a\tb",
            "refs/heads/a~1", "refs/heads/a^", "refs/heads/a:b", "refs/heads/a?", "refs/heads/a*", "refs/heads/a[b",
            "refs/heads/a\\b", "refs/heads/x/", "refs/heads/a\x7fb", "head",
        ]
        for name in bad:
            with self.assertRaises(MinigitError, msg=repr(name)):
                check_ref_name(name)

    def test_helpers(self):
        self.assertTrue(is_hex_sha("a" * 40))
        self.assertFalse(is_hex_sha("A" * 40))
        self.assertFalse(is_hex_sha("a" * 39))
        self.assertFalse(is_hex_sha("g" * 40))
        self.assertEqual(short_ref("refs/heads/x"), "x")
        self.assertEqual(short_ref("refs/heads/feat/x"), "feat/x")
        self.assertEqual(short_ref("refs/remotes/origin/x"), "origin/x")
        self.assertEqual(short_ref("refs/tags/v1"), "tags/v1")
        self.assertEqual(short_ref("HEAD"), "HEAD")


class RefStoreTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.gitdir = self.p("gd")
        os.makedirs(os.path.join(self.gitdir, "refs", "heads"))
        self.refs = RefStore(self.gitdir)
        atomic_write(os.path.join(self.gitdir, "HEAD"), b"ref: refs/heads/main\n")
        self.A, self.B, self.C = "a" * 40, "b" * 40, "c" * 40

    def test_cas_create_mismatch_delete(self):
        r = self.refs
        self.assertIsNone(r.resolve("refs/heads/main"))
        self.assertIsNone(r.resolve("HEAD"))
        r.update("refs/heads/main", self.A, expected=None)
        with open(os.path.join(self.gitdir, "refs", "heads", "main"), "rb") as f:
            self.assertEqual(f.read(), self.A.encode() + b"\n")
        self.assertEqual(r.resolve("refs/heads/main"), self.A)
        self.assertEqual(r.resolve("HEAD"), self.A)
        self.assertEqual(r.read_raw("HEAD"), "ref: refs/heads/main")
        with self.assertRaises(RefUpdateError) as cm:
            r.update("refs/heads/main", self.B, expected=None)
        self.assertEqual((cm.exception.ref, cm.exception.actual), ("refs/heads/main", self.A))
        with self.assertRaises(RefUpdateError) as cm:
            r.update("refs/heads/main", self.B, expected=self.C)
        self.assertEqual((cm.exception.expected, cm.exception.actual), (self.C, self.A))
        self.assertEqual(r.resolve("refs/heads/main"), self.A)
        r.update("refs/heads/main", self.B, expected=self.A)
        self.assertEqual(r.resolve("refs/heads/main"), self.B)
        r.update("refs/heads/main", self.C)  # ANY
        self.assertEqual(r.resolve("refs/heads/main"), self.C)
        with self.assertRaises(RefUpdateError):
            r.update("refs/heads/main", None, expected=self.A)
        with self.assertRaises(RefUpdateError):
            r.update("refs/heads/nope", None, expected=self.A)
        r.update("refs/heads/main", None, expected=self.C)
        self.assertIsNone(r.resolve("refs/heads/main"))
        self.assertFalse(os.path.exists(os.path.join(self.gitdir, "refs", "heads", "main")))
        self.assertFalse(os.path.exists(os.path.join(self.gitdir, "refs", "heads", "main.lock")))
        r.update("refs/heads/gone", None)  # deleting a missing ref with ANY is fine
        with self.assertRaises(MinigitError):
            r.update("refs/heads/main", "nothex")
        with self.assertRaises(MinigitError):
            r.update("heads/main", self.A)

    def test_delete_prunes_dirs(self):
        r = self.refs
        r.update("refs/heads/feat/deep/x", self.A, expected=None)
        r.update("refs/remotes/origin/feat/y", self.A, expected=None)
        self.assertEqual(r.list(), {"refs/heads/feat/deep/x": self.A, "refs/remotes/origin/feat/y": self.A})
        r.update("refs/heads/feat/deep/x", None, expected=self.A)
        self.assertFalse(os.path.exists(os.path.join(self.gitdir, "refs", "heads", "feat")))
        self.assertTrue(os.path.isdir(os.path.join(self.gitdir, "refs", "heads")))
        r.update("refs/remotes/origin/feat/y", None)
        self.assertFalse(os.path.exists(os.path.join(self.gitdir, "refs", "remotes", "origin", "feat")))
        self.assertTrue(os.path.isdir(os.path.join(self.gitdir, "refs", "remotes", "origin")))
        self.assertEqual(r.list(), {})

    def test_file_dir_clash(self):
        r = self.refs
        r.update("refs/heads/a", self.A)
        with self.assertRaises(RefUpdateError):
            r.update("refs/heads/a/b", self.B)
        r.update("refs/heads/c/d", self.A)
        with self.assertRaises(RefUpdateError):
            r.update("refs/heads/c", self.B)
        self.assertEqual(r.list("refs/heads/"), {"refs/heads/a": self.A, "refs/heads/c/d": self.A})

    def test_held_lock_raises_lockerror(self):
        r = self.refs
        r.update("refs/heads/main", self.A)
        with LockFile(os.path.join(self.gitdir, "refs", "heads", "main")):
            with self.assertRaises(LockError):
                r.update("refs/heads/main", self.B, expected=self.A)
            with self.assertRaises(LockError):
                r.update("refs/heads/main", self.B, expected=self.A, timeout=0.1)
        self.assertEqual(r.resolve("refs/heads/main"), self.A)
        r.update("refs/heads/main", self.B, expected=self.A)
        self.assertEqual(r.resolve("refs/heads/main"), self.B)

    def test_concurrent_cas_one_winner_per_round(self):
        r = self.refs
        ref = "refs/heads/race"
        r.update(ref, "0" * 40, expected=None)
        n = 16
        for rnd in range(3):
            cur = r.resolve(ref)
            barrier = threading.Barrier(n)
            results: list[str] = []
            lock = threading.Lock()

            def worker(i: int, rnd: int = rnd) -> None:
                # Values must differ from every earlier value of the ref: if a thread
                # rewrote the current value (ABA), a second thread would rightly win too.
                new = ("%02x%02x" % (rnd + 1, i)) * 10
                barrier.wait()
                try:
                    r.update(ref, new, expected=cur, timeout=10.0)
                    with lock:
                        results.append("ok:" + new)
                except RefUpdateError:
                    with lock:
                        results.append("lost")
                except Exception as e:  # pragma: no cover - makes failures visible
                    with lock:
                        results.append("error:%r" % e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            winners = [x for x in results if x.startswith("ok:")]
            self.assertEqual(len(results), n, results)
            self.assertEqual(len(winners), 1, "round %d: %r" % (rnd, results))
            self.assertEqual(results.count("lost"), n - 1, results)
            self.assertEqual(r.resolve(ref), winners[0][3:])
            self.assertFalse(os.path.exists(os.path.join(self.gitdir, "refs", "heads", "race.lock")))

    def test_head_helpers(self):
        r = self.refs
        self.assertEqual(r.head_target(), "refs/heads/main")
        self.assertEqual(r.current_branch(), "main")
        self.assertIsNone(r.resolve("HEAD"))
        r.update_head(self.A, expected=None)  # unborn branch is allowed
        self.assertEqual(r.resolve("refs/heads/main"), self.A)
        with self.assertRaises(RefUpdateError):
            r.update_head(self.B, expected=self.C)
        r.update_head(self.B, expected=self.A)
        self.assertEqual(r.resolve("HEAD"), self.B)
        r.set_detached_head(self.C)
        self.assertIsNone(r.head_target())
        self.assertIsNone(r.current_branch())
        self.assertEqual(r.read_raw("HEAD"), self.C)
        self.assertEqual(r.resolve("HEAD"), self.C)
        r.update_head(self.A, expected=self.C)
        self.assertEqual(r.read_raw("HEAD"), self.A)
        self.assertEqual(r.resolve("refs/heads/main"), self.B)  # branch untouched while detached
        r.set_symbolic_head("refs/heads/other")
        self.assertEqual(r.current_branch(), "other")
        self.assertIsNone(r.resolve("HEAD"))
        with self.assertRaises(MinigitError):
            r.set_symbolic_head("other")
        with self.assertRaises(MinigitError):
            r.set_detached_head("nope")

    def test_list_and_specials(self):
        r = self.refs
        r.update("refs/heads/zeta", self.A)
        r.update("refs/heads/alpha", self.B)
        r.update("refs/remotes/origin/main", self.C)
        atomic_write(os.path.join(self.gitdir, "refs", "heads", "junk.lock"), b"x")
        self.assertEqual(list(r.list()), ["refs/heads/alpha", "refs/heads/zeta", "refs/remotes/origin/main"])
        self.assertEqual(r.list("refs/heads/"), {"refs/heads/alpha": self.B, "refs/heads/zeta": self.A})
        self.assertEqual(r.list("refs/remotes/origin/"), {"refs/remotes/origin/main": self.C})
        self.assertEqual(r.list("refs/tags/"), {})
        self.assertEqual(RefStore(self.p("nothing")).list(), {})
        self.assertIsNone(r.read_special("ORIG_HEAD"))
        r.write_special("ORIG_HEAD", self.A)
        self.assertEqual(r.read_special("ORIG_HEAD"), self.A)
        with open(os.path.join(self.gitdir, "ORIG_HEAD"), "rb") as f:
            self.assertEqual(f.read(), self.A.encode() + b"\n")
        r.write_special("ORIG_HEAD", None)
        r.write_special("ORIG_HEAD", None)
        self.assertIsNone(r.read_special("ORIG_HEAD"))
        self.assertIsNone(r.read_raw("refs/heads/alpha/sub"))  # NotADirectory -> None

    def test_resolve_symref_loop_is_safe(self):
        atomic_write(os.path.join(self.gitdir, "refs", "heads", "loop"), b"ref: refs/heads/loop\n")
        self.assertIsNone(self.refs.resolve("refs/heads/loop"))


# ---------------------------------------------------------------------------
# repo
# ---------------------------------------------------------------------------


class RepoTest(TempDirCase):
    def test_init_layout_and_reinit(self):
        root = self.p("work")
        repo, reinit = Repository.init(root)
        self.assertFalse(reinit)
        self.assertFalse(repo.bare)
        self.assertEqual(repo.worktree, root)
        self.assertEqual(repo.gitdir, os.path.join(root, ".minigit"))
        for sub in ("objects", os.path.join("refs", "heads"), os.path.join("refs", "remotes")):
            self.assertTrue(os.path.isdir(repo.gpath(sub)), sub)
        with open(repo.gpath("HEAD"), "rb") as f:
            self.assertEqual(f.read(), b"ref: refs/heads/main\n")
        with open(repo.gpath("config"), "rb") as f:
            self.assertEqual(f.read(), b"[core]\n\tbare = false\n")
        self.assertFalse(repo.config.get_bool("core.bare"))
        self.assertEqual(repo.refs.current_branch(), "main")
        self.assertIsNone(repo.head_commit())
        self.assertEqual(repo.head_flat(), {})
        self.assertIsNone(repo.merge_head())
        self.assertEqual(repo.require_worktree(), root)
        repo.config.set("user.name", "Keep")
        repo2, reinit = Repository.init(root)
        self.assertTrue(reinit)
        self.assertEqual(repo2.config.get("user.name"), "Keep")
        repo3, _ = Repository.init(self.p("other"), initial_branch="trunk")
        self.assertEqual(repo3.refs.head_target(), "refs/heads/trunk")
        with self.assertRaises(MinigitError):
            Repository.init(self.p("bad"), initial_branch="bad name")

    def test_bare_init_open_discover(self):
        bare_dir = self.p("proj.git")
        repo, reinit = Repository.init(bare_dir, bare=True)
        self.assertFalse(reinit)
        self.assertTrue(repo.bare)
        self.assertIsNone(repo.worktree)
        self.assertEqual(repo.gitdir, bare_dir)
        self.assertTrue(repo.config.get_bool("core.bare"))
        self.assertTrue(os.path.isfile(os.path.join(bare_dir, "HEAD")))
        self.assertFalse(os.path.exists(os.path.join(bare_dir, ".minigit")))
        with self.assertRaises(MinigitError):
            repo.require_worktree()
        with self.assertRaises(MinigitError):
            repo.relpath("x", bare_dir)
        self.assertTrue(Repository.init(bare_dir, bare=True)[1])
        opened = Repository.open(bare_dir)
        self.assertTrue(opened.bare)
        os.makedirs(os.path.join(bare_dir, "objects", "ab"))
        found = Repository.discover(os.path.join(bare_dir, "objects", "ab"))
        self.assertTrue(found.bare)
        self.assertEqual(found.gitdir, bare_dir)

    def test_discover_and_open(self):
        root = self.p("work")
        Repository.init(root)
        sub = os.path.join(root, "a", "b")
        os.makedirs(sub)
        repo = Repository.discover(sub)
        self.assertEqual(repo.worktree, root)
        self.assertEqual(Repository.discover(root).gitdir, repo.gitdir)
        self.assertEqual(Repository.discover(repo.gitdir).worktree, root)
        self.assertEqual(Repository.open(root).worktree, root)
        outside = self.p("elsewhere")
        os.makedirs(outside)
        with self.assertRaises(NotARepositoryError) as cm:
            Repository.discover(outside)
        self.assertIn("not a minigit repository (or any parent): " + outside, cm.exception.message)
        with self.assertRaises(NotARepositoryError):
            Repository.open(outside)
        # nested repo wins over the outer one
        inner = os.path.join(root, "inner")
        Repository.init(inner)
        self.assertEqual(Repository.discover(os.path.join(inner)).worktree, inner)

    def test_relpath_and_abspath(self):
        root = self.p("work")
        repo, _ = Repository.init(root)
        sub = os.path.join(root, "dir", "sub")
        os.makedirs(sub)
        self.assertEqual(repo.relpath(".", root), "")
        self.assertEqual(repo.relpath("", root), "")
        self.assertEqual(repo.relpath("a.txt", root), "a.txt")
        self.assertEqual(repo.relpath("./dir//x.txt", root), "dir/x.txt")
        self.assertEqual(repo.relpath("x.txt", sub), "dir/sub/x.txt")
        self.assertEqual(repo.relpath("..", sub), "dir")
        self.assertEqual(repo.relpath("../../", sub), "")
        self.assertEqual(repo.relpath(os.path.join(root, "dir", "y"), self.tmp), "dir/y")
        self.assertEqual(repo.relpath("dir\\sub\\z" if os.name == "nt" else "dir/sub/z", root), "dir/sub/z")
        if os.name == "nt":
            self.assertEqual(repo.relpath("x", root.lower()), "x")
            self.assertEqual(repo.relpath("x", root.upper()), "x")
        for bad in ("..", "../other", self.tmp):
            with self.assertRaises(MinigitError, msg=bad) as cm:
                repo.relpath(bad, root)
            self.assertIn("is outside repository", cm.exception.message)
        self.assertEqual(repo.abspath(""), root)
        self.assertEqual(repo.abspath("dir/sub/z"), os.path.join(root, "dir", "sub", "z"))

    def test_identity(self):
        repo, _ = Repository.init(self.p("work"))
        with self.assertRaises(MinigitError) as cm:
            repo.identity()
        self.assertIn("please set your identity", cm.exception.message)
        repo.config.set("user.name", "Ann")
        with self.assertRaises(MinigitError):
            repo.identity()
        repo.config.set("user.email", "ann@example.com", scope="global")
        sig = repo.identity()
        self.assertEqual((sig.name, sig.email), ("Ann", "ann@example.com"))
        self.assertLessEqual(abs(sig.timestamp - int(time.time())), 5)
        self.assertEqual(sig.tz_offset, time.localtime().tm_gmtoff)
        os.environ["MINIGIT_AUTHOR_NAME"] = "Env"
        os.environ["MINIGIT_AUTHOR_EMAIL"] = "env@example.com"
        os.environ["MINIGIT_AUTHOR_DATE"] = "1700000000 +0530"
        self.assertEqual(repo.identity(), Signature("Env", "env@example.com", 1700000000, 19800))
        os.environ["MINIGIT_AUTHOR_DATE"] = "garbage"
        with self.assertRaises(MinigitError):
            repo.identity()

    def test_state_files_and_read_commit(self):
        repo, _ = Repository.init(self.p("work"))
        self.assertIsNone(repo.read_state("MERGE_MSG"))
        repo.write_state("MERGE_MSG", b"Merge branch 'x'\n\n# Conflicts:\n#\ta\n")
        self.assertEqual(repo.read_state("MERGE_MSG"), b"Merge branch 'x'\n\n# Conflicts:\n#\ta\n")
        repo.remove_state("MERGE_MSG")
        repo.remove_state("MERGE_MSG")
        self.assertIsNone(repo.read_state("MERGE_MSG"))
        blob = repo.objects.write("blob", b"hi\n")
        tree = build_tree(repo.objects, {"f": ("100644", blob)})
        c = make_commit(repo.objects, tree, [], 10, "first")
        repo.refs.update_head(c, expected=None)
        self.assertEqual(repo.head_commit(), c)
        self.assertEqual(repo.head_flat(), {"f": ("100644", blob)})
        self.assertEqual(repo.read_commit(c).subject, "first")
        repo.refs.write_special("MERGE_HEAD", c)
        self.assertEqual(repo.merge_head(), c)
        with self.assertRaises(ObjectError):
            repo.read_commit(blob)


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


class GraphTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = ObjectStore(self.p("objects"))
        self.tree = build_tree(self.store, {})

    def c(self, parents, ts, msg="m"):
        return make_commit(self.store, self.tree, parents, ts, msg)

    def test_parents_and_time(self):
        a = self.c([], 1)
        b = self.c([a], 2)
        self.assertEqual(graph.parents(self.store, a), ())
        self.assertEqual(graph.parents(self.store, b), (a,))
        self.assertEqual(graph.commit_time(self.store, b), 2)

    def test_linear(self):
        a = self.c([], 1)
        b = self.c([a], 2)
        c = self.c([b], 3)
        self.assertEqual(graph.ancestors(self.store, [c]), {a, b, c})
        self.assertEqual(graph.ancestors(self.store, []), set())
        self.assertTrue(graph.is_ancestor(self.store, a, c))
        self.assertTrue(graph.is_ancestor(self.store, c, c))
        self.assertFalse(graph.is_ancestor(self.store, c, a))
        self.assertEqual(graph.merge_bases(self.store, a, c), [a])
        self.assertEqual(graph.merge_bases(self.store, c, a), [a])
        self.assertEqual(graph.merge_bases(self.store, c, c), [c])
        self.assertEqual(graph.best_merge_base(self.store, b, c), b)
        self.assertEqual(graph.ahead_behind(self.store, c, a), (2, 0))
        self.assertEqual(graph.ahead_behind(self.store, a, c), (0, 2))
        self.assertEqual(graph.ahead_behind(self.store, c, c), (0, 0))

    def test_fork(self):
        base = self.c([], 1)
        l1 = self.c([base], 2)
        l2 = self.c([l1], 3)
        r1 = self.c([base], 4)
        self.assertEqual(graph.merge_bases(self.store, l2, r1), [base])
        self.assertEqual(graph.best_merge_base(self.store, l2, r1), base)
        self.assertFalse(graph.is_ancestor(self.store, l2, r1))
        self.assertEqual(graph.ahead_behind(self.store, l2, r1), (2, 1))
        m = self.c([l2, r1], 5)
        self.assertEqual(graph.merge_bases(self.store, m, r1), [r1])
        self.assertEqual(graph.merge_bases(self.store, m, l1), [l1])
        self.assertTrue(graph.is_ancestor(self.store, r1, m))

    def test_criss_cross(self):
        base = self.c([], 1)
        a1 = self.c([base], 2, "a1")
        b1 = self.c([base], 3, "b1")
        a2 = self.c([a1, b1], 4, "a2")
        b2 = self.c([b1, a1], 5, "b2")
        bases = graph.merge_bases(self.store, a2, b2)
        self.assertEqual(sorted(bases), sorted([a1, b1]))
        self.assertEqual(graph.best_merge_base(self.store, a2, b2), b1)  # newest committer time
        # tie on time -> lowest sha
        x1 = self.c([base], 9, "x1")
        y1 = self.c([base], 9, "y1")
        x2 = self.c([x1, y1], 10)
        y2 = self.c([y1, x1], 11)
        self.assertEqual(graph.best_merge_base(self.store, x2, y2), min(x1, y1))

    def test_unrelated(self):
        a = self.c([], 1, "a")
        b = self.c([], 2, "b")
        a2 = self.c([a], 3)
        self.assertEqual(graph.merge_bases(self.store, a2, b), [])
        self.assertIsNone(graph.best_merge_base(self.store, a2, b))
        self.assertFalse(graph.is_ancestor(self.store, b, a2))
        self.assertEqual(graph.ahead_behind(self.store, a2, b), (2, 1))

    def test_topo_walk_equal_timestamps(self):
        chain = [self.c([], 7, "c0")]
        for i in range(1, 6):
            chain.append(self.c([chain[-1]], 7, "c%d" % i))
        order = [sha for sha, _ in graph.topo_walk(self.store, [chain[-1]])]
        self.assertEqual(order, list(reversed(chain)))
        subjects = [c.subject for _, c in graph.topo_walk(self.store, [chain[-1]])]
        self.assertEqual(subjects, ["c5", "c4", "c3", "c2", "c1", "c0"])
        # a parent with a *newer* timestamp than its child still comes after it
        child = self.c([chain[-1]], 1, "old-child")
        order = [sha for sha, _ in graph.topo_walk(self.store, [child])]
        self.assertEqual(order[0], child)
        self.assertEqual(order[1:], list(reversed(chain)))

    def test_topo_walk_merge_and_multiple_starts(self):
        base = self.c([], 1, "base")
        left = self.c([base], 3, "left")
        right = self.c([base], 2, "right")
        merge = self.c([left, right], 4, "merge")
        order = [c.subject for _, c in graph.topo_walk(self.store, [merge])]
        self.assertEqual(order, ["merge", "left", "right", "base"])
        # equal timestamps: first parent (insertion order) first
        l2 = self.c([base], 5, "l2")
        r2 = self.c([base], 5, "r2")
        m2 = self.c([r2, l2], 6, "m2")
        self.assertEqual([c.subject for _, c in graph.topo_walk(self.store, [m2])], ["m2", "r2", "l2", "base"])
        # multiple starts, duplicates ignored, each commit once
        order = [c.subject for _, c in graph.topo_walk(self.store, [left, right, right, base])]
        self.assertEqual(order, ["left", "right", "base"])
        order = [c.subject for _, c in graph.topo_walk(self.store, [right, merge])]
        self.assertEqual(order, ["merge", "left", "right", "base"])
        self.assertEqual(list(graph.topo_walk(self.store, [])), [])


# ---------------------------------------------------------------------------
# revparse
# ---------------------------------------------------------------------------


class RevparseTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo, _ = Repository.init(self.p("work"))
        st = self.repo.objects
        self.blob = st.write("blob", b"hello\n")
        self.blob2 = st.write("blob", b"other\n")
        self.tree1 = build_tree(st, {"a.txt": ("100644", self.blob), "dir/b.txt": ("100644", self.blob2)})
        self.tree2 = build_tree(st, {"a.txt": ("100644", self.blob2)})
        self.c1 = make_commit(st, self.tree1, [], 1, "one")
        self.c2 = make_commit(st, self.tree2, [self.c1], 2, "two")
        self.c3 = make_commit(st, self.tree2, [self.c2], 3, "three")
        self.side = make_commit(st, self.tree1, [self.c1], 4, "side")
        self.merge = make_commit(st, self.tree2, [self.c3, self.side], 5, "merge")
        self.repo.refs.update("refs/heads/main", self.merge)
        self.repo.refs.update("refs/heads/side", self.side)
        self.repo.refs.update("refs/remotes/origin/main", self.c3)
        self.repo.refs.update("refs/tags/v1", self.c2)

    def test_abbrev(self):
        self.assertEqual(revparse.abbrev("0123456789" * 4), "0123456")
        self.assertEqual(revparse.abbrev("0123456789" * 4, 4), "0123")

    def test_resolve_ref_and_local_branch(self):
        r = self.repo
        self.assertEqual(revparse.resolve_ref(r, "main"), "refs/heads/main")
        self.assertEqual(revparse.resolve_ref(r, "origin/main"), "refs/remotes/origin/main")
        self.assertEqual(revparse.resolve_ref(r, "refs/tags/v1"), "refs/tags/v1")
        self.assertEqual(revparse.resolve_ref(r, "refs/heads/side"), "refs/heads/side")
        self.assertIsNone(revparse.resolve_ref(r, "nope"))
        self.assertIsNone(revparse.resolve_ref(r, ""))
        self.assertEqual(revparse.resolve_local_branch(r, "side"), "refs/heads/side")
        self.assertIsNone(revparse.resolve_local_branch(r, "origin/main"))
        self.assertIsNone(revparse.resolve_local_branch(r, "v1"))

    def test_base_names(self):
        r = self.repo
        self.assertEqual(revparse.resolve_rev(r, "HEAD"), self.merge)
        self.assertEqual(revparse.resolve_rev(r, "@"), self.merge)
        self.assertEqual(revparse.resolve_rev(r, "main"), self.merge)
        self.assertEqual(revparse.resolve_rev(r, "side"), self.side)
        self.assertEqual(revparse.resolve_rev(r, "origin/main"), self.c3)
        self.assertEqual(revparse.resolve_rev(r, "refs/tags/v1"), self.c2)
        self.assertEqual(revparse.resolve_rev(r, self.c1), self.c1)
        self.assertEqual(revparse.resolve_rev(r, self.c1[:7]), self.c1)
        self.assertEqual(revparse.resolve_rev(r, self.c1[:7].upper()), self.c1)
        self.assertEqual(revparse.resolve_object(r, self.blob[:10]), self.blob)
        r.refs.write_special("ORIG_HEAD", self.c2)
        r.refs.write_special("MERGE_HEAD", self.side)
        self.assertEqual(revparse.resolve_rev(r, "ORIG_HEAD"), self.c2)
        self.assertEqual(revparse.resolve_rev(r, "MERGE_HEAD"), self.side)
        r.refs.write_special("MERGE_HEAD", None)
        with self.assertRaises(RevisionError):
            revparse.resolve_rev(r, "MERGE_HEAD")
        # detached HEAD
        r.refs.set_detached_head(self.c2)
        self.assertEqual(revparse.resolve_rev(r, "HEAD"), self.c2)

    def test_suffixes(self):
        r = self.repo
        self.assertEqual(revparse.resolve_rev(r, "HEAD~1"), self.c3)
        self.assertEqual(revparse.resolve_rev(r, "HEAD~"), self.c3)
        self.assertEqual(revparse.resolve_rev(r, "HEAD^"), self.c3)
        self.assertEqual(revparse.resolve_rev(r, "HEAD^1"), self.c3)
        self.assertEqual(revparse.resolve_rev(r, "HEAD^2"), self.side)
        self.assertEqual(revparse.resolve_rev(r, "HEAD^2~1"), self.c1)
        self.assertEqual(revparse.resolve_rev(r, "HEAD~2"), self.c2)
        self.assertEqual(revparse.resolve_rev(r, "main~3"), self.c1)
        self.assertEqual(revparse.resolve_rev(r, "HEAD^^"), self.c2)
        self.assertEqual(revparse.resolve_rev(r, "HEAD^0"), self.merge)
        self.assertEqual(revparse.resolve_rev(r, self.merge[:8] + "^2"), self.side)
        for bad in ("HEAD~4", "HEAD^3", "side^2", "HEAD~x", "HEAD~1~x", "HEAD^-1"):
            with self.assertRaises(RevisionError, msg=bad) as cm:
                revparse.resolve_rev(r, bad)
            self.assertIn("unknown revision", cm.exception.message)

    def test_errors(self):
        r = self.repo
        with self.assertRaises(RevisionError) as cm:
            revparse.resolve_rev(r, "nope")
        self.assertEqual(cm.exception.message, "unknown revision 'nope'")
        with self.assertRaises(RevisionError):
            revparse.resolve_rev(r, "")
        with self.assertRaises(RevisionError):
            revparse.resolve_rev(r, "f" * 40)  # well-formed, missing
        with self.assertRaises(RevisionError):
            revparse.resolve_rev(r, "abc")  # too short
        with self.assertRaises(RevisionError) as cm:
            revparse.resolve_rev(r, self.blob)
        self.assertEqual(cm.exception.message, "'%s' is not a commit" % self.blob)
        with self.assertRaises(RevisionError) as cm:
            revparse.resolve_rev(r, self.blob[:8] + "~1")
        self.assertIn("is not a commit", cm.exception.message)
        with self.assertRaises(RevisionError) as cm:
            revparse.resolve_rev(r, "HEAD:a.txt")
        self.assertIn("is not a commit", cm.exception.message)
        # unborn HEAD
        empty, _ = Repository.init(self.p("empty"))
        with self.assertRaises(RevisionError) as cm:
            revparse.resolve_rev(empty, "HEAD")
        self.assertEqual(cm.exception.message, "unknown revision 'HEAD'")

    def test_ambiguous_prefix(self):
        st = self.repo.objects
        seen: dict[str, str] = {}
        prefix = None
        for i in range(5000):
            sha = st.write("blob", b"amb-%d" % i)
            if sha[:4] in seen:
                prefix = sha[:4]
                break
            seen[sha[:4]] = sha
        self.assertIsNotNone(prefix, "no 4-char collision found")
        matches = st.resolve_prefix(prefix)
        self.assertGreaterEqual(len(matches), 2)
        with self.assertRaises(RevisionError) as cm:
            revparse.resolve_object(self.repo, prefix)
        self.assertEqual(cm.exception.message, "ambiguous short id '%s' (matches %d objects)" % (prefix, len(matches)))
        # a longer unique prefix still works
        full = matches[0]
        n = 5
        while len(st.resolve_prefix(full[:n])) > 1:
            n += 1
        self.assertEqual(revparse.resolve_object(self.repo, full[:n]), full)

    def test_rev_path(self):
        r = self.repo
        self.assertEqual(revparse.resolve_object(r, "HEAD:a.txt"), self.blob2)
        self.assertEqual(revparse.resolve_object(r, "HEAD~3:a.txt"), self.blob)
        self.assertEqual(revparse.resolve_object(r, "side:dir/b.txt"), self.blob2)
        self.assertEqual(revparse.resolve_object(r, "side:dir"), flatten_dir_sha(r, self.tree1, "dir"))
        self.assertEqual(revparse.resolve_object(r, "side:"), self.tree1)
        self.assertEqual(revparse.resolve_object(r, self.tree1 + ":a.txt"), self.blob)
        for bad, msg in (("HEAD:dir/b.txt", "path 'dir/b.txt' does not exist in 'HEAD'"),
                         ("side:zzz", "path 'zzz' does not exist in 'side'"),
                         ("side:a.txt/x", "path 'a.txt/x' does not exist in 'side'")):
            with self.assertRaises(RevisionError, msg=bad) as cm:
                revparse.resolve_object(r, bad)
            self.assertEqual(cm.exception.message, msg)
        with self.assertRaises(RevisionError):
            revparse.resolve_object(r, ":a.txt")
        with self.assertRaises(RevisionError):
            revparse.resolve_object(r, self.blob + ":x")


def flatten_dir_sha(repo: Repository, tree_sha: str, name: str) -> str:
    for e in parse_tree(repo.objects.read_typed(tree_sha, "tree")):
        if e.name == name:
            return e.sha
    raise AssertionError(name)


if __name__ == "__main__":
    unittest.main()

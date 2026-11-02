"""tests.test_tags -- owner: tags (features/tags.md).

Unit tests for tag objects (git-identical), ``refs/tags/`` resolution and peeling in revparse,
the ``tag`` command (through ``cli.main`` with the real modules), log decorations, ``show`` of an
annotated tag, and tags in packs, connectivity and fsck.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest

from minigit import cli, history, ops, pack, tags
from minigit.errors import MinigitError, ObjectError, RevisionError
from minigit.merge import default_merge_message
from minigit.objects import ObjectStore, Signature, Tag, hash_object, parse_tag, peel, serialize_tag
from minigit.repo import Repository
from minigit.revparse import resolve_object, resolve_rev
from minigit.util import format_date

ENV_KEYS = ("MINIGIT_GLOBAL_CONFIG", "MINIGIT_AUTHOR_NAME", "MINIGIT_AUTHOR_EMAIL", "MINIGIT_AUTHOR_DATE")

# Made with real git: a commit 0bf639c..., then
#   GIT_COMMITTER_DATE="1700000060 +0530" git tag -a v1 -m "first release"
GIT_COMMIT = "0bf639c53571e75b4f0c41342850f7cd9c4d3a1a"
GIT_TAG_SHA = "323dedec36b08e615d473a32b7a5706a5405e47c"
GIT_TAG_BYTES = (b"object 0bf639c53571e75b4f0c41342850f7cd9c4d3a1a\ntype commit\ntag v1\n"
                 b"tagger Tester <t@example.com> 1700000060 +0530\n\nfirst release\n")


class TagObjectTests(unittest.TestCase):
    def test_git_identical_serialization_and_sha(self):
        tag = Tag(GIT_COMMIT, "commit", "v1", Signature("Tester", "t@example.com", 1700000060, 19800),
                  "first release\n")
        self.assertEqual(serialize_tag(tag), GIT_TAG_BYTES)
        self.assertEqual(hash_object("tag", GIT_TAG_BYTES), GIT_TAG_SHA)
        # a message without a final newline gets exactly one
        self.assertEqual(serialize_tag(Tag(GIT_COMMIT, "commit", "v1", tag.tagger, "first release")),
                         GIT_TAG_BYTES)

    def test_parse(self):
        tag = parse_tag(GIT_TAG_BYTES)
        self.assertEqual((tag.object, tag.type, tag.tag), (GIT_COMMIT, "commit", "v1"))
        self.assertEqual((tag.tagger.name, tag.tagger.email, tag.tagger.timestamp, tag.tagger.tz_offset),
                         ("Tester", "t@example.com", 1700000060, 19800))
        self.assertEqual(tag.message, "first release\n")
        self.assertEqual(tag.subject, "first release")
        # tagger is optional (very old git tags), unknown headers are dropped
        tag = parse_tag(b"object " + GIT_COMMIT.encode() + b"\ntype commit\ntag x\nfoo bar\n\nmsg\n")
        self.assertIsNone(tag.tagger)
        self.assertEqual(tag.message, "msg\n")

    def test_parse_rejects_malformed(self):
        for bad in (b"type commit\ntag v1\n\nm\n",                                   # no object
                    b"object " + GIT_COMMIT.encode() + b"\ntag v1\n\nm\n",             # no type
                    b"object " + GIT_COMMIT.encode() + b"\ntype commit\n\nm\n",        # no name
                    b"object xyz\ntype commit\ntag v1\n\nm\n",                         # bad sha
                    b"object " + GIT_COMMIT.encode() + b"\ntype note\ntag v1\n\nm\n",  # bad type
                    b"object " + GIT_COMMIT.encode() + b"\ntype commit\ntag v1\nnospace\n\nm\n"):
            with self.assertRaises(ObjectError, msg=repr(bad)):
                parse_tag(bad)

    def test_store_refuses_malformed_tags_and_peels(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = ObjectStore(os.path.join(tmp, "objects"))
            with self.assertRaises(ObjectError):
                store.write("tag", b"garbage")
            blob = store.write("blob", b"x")
            t1 = store.write("tag", serialize_tag(Tag(blob, "blob", "t1", None, "m\n")))
            t2 = store.write("tag", serialize_tag(Tag(t1, "tag", "t2", None, "m\n")))
            self.assertEqual(peel(store, t2), ("blob", blob))
            self.assertEqual(peel(store, blob), ("blob", blob))
            with self.assertRaises(ObjectError):
                peel(store, "a" * 40)


class RepoCase(unittest.TestCase):
    """A temp work tree with a fixed identity, driven through the real library and ``cli.main``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["MINIGIT_GLOBAL_CONFIG"] = os.path.join(self.root, "globalconfig")
        os.environ["MINIGIT_AUTHOR_NAME"] = "Test User"
        os.environ["MINIGIT_AUTHOR_EMAIL"] = "test@example.com"
        os.environ["MINIGIT_AUTHOR_DATE"] = "1700000000 +0000"
        self.clock = 1700000000
        self.wt = os.path.join(self.root, "wt")
        os.makedirs(self.wt)
        ops.init(self.wt)
        self.repo = Repository.open(self.wt)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def commit_file(self, rel: str, data: str, msg: str) -> str:
        p = os.path.join(self.wt, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data.encode())
        ops.add(self.repo, [rel], self.wt)
        self.clock += 60
        os.environ["MINIGIT_AUTHOR_DATE"] = f"{self.clock} +0000"
        sha, _ = ops.commit(self.repo, [msg])
        return sha

    def cli(self, *args):
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["-C", self.wt, *args])
        out.flush()
        err.flush()
        return rc, out.buffer.getvalue().decode("utf-8"), err.buffer.getvalue().decode("utf-8")

    def ok(self, *args) -> str:
        rc, out, err = self.cli(*args)
        self.assertEqual(rc, 0, f"{args}: stdout={out!r} stderr={err!r}")
        return out

    def fail_cli(self, *args, code=1) -> str:
        rc, out, err = self.cli(*args)
        self.assertEqual(rc, code, f"{args}: stdout={out!r} stderr={err!r}")
        return err


class TagCommandTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.c1 = self.commit_file("a.txt", "1\n", "one")
        self.c2 = self.commit_file("a.txt", "2\n", "two")

    def test_lightweight_create_list_delete(self):
        self.assertEqual(self.ok("tag"), "")
        self.assertEqual(self.ok("tag", "v1", "HEAD~1"), "")
        self.assertEqual(self.ok("tag", "v2"), "")
        self.assertEqual(self.repo.refs.resolve("refs/tags/v1"), self.c1)
        self.assertEqual(self.repo.refs.resolve("refs/tags/v2"), self.c2)
        self.assertEqual(self.ok("tag"), "v1\nv2\n")
        self.assertEqual(self.ok("tag", "-l", "v*"), "v1\nv2\n")
        self.assertEqual(self.ok("tag", "-l", "*2"), "v2\n")
        self.assertEqual(self.ok("tag", "-l", "nope*"), "")
        self.assertEqual(self.ok("tag", "-n"), "v1              one\nv2              two\n")
        self.assertEqual(self.ok("tag", "-d", "v1"), f"Deleted tag 'v1' (was {self.c1[:7]})\n")
        self.assertIsNone(self.repo.refs.resolve("refs/tags/v1"))
        self.assertEqual(self.ok("tag"), "v2\n")
        self.assertIn("error: tag 'v1' not found.", self.fail_cli("tag", "-d", "v1"))
        # a plain file name is not an object (``cat-file`` sees a commit)
        self.assertEqual(self.ok("cat-file", "-t", "v2"), "commit\n")

    def test_annotated_tag_object(self):
        out = self.ok("tag", "-a", "v1", "-m", "first release", "-m", "details here")
        self.assertEqual(out, "")
        ref_sha = self.repo.refs.resolve("refs/tags/v1")
        self.assertEqual(self.repo.objects.read(ref_sha)[0], "tag")
        tag = tags.read_tag(self.repo, ref_sha)
        self.assertEqual((tag.object, tag.type, tag.tag), (self.c2, "commit", "v1"))
        self.assertEqual(tag.message, "first release\n\ndetails here\n")
        self.assertEqual((tag.tagger.name, tag.tagger.email, tag.tagger.timestamp),
                         ("Test User", "test@example.com", self.clock))
        self.assertEqual(self.ok("tag", "-n"), "v1              first release\n")
        self.assertEqual(self.ok("cat-file", "-t", "v1"), "tag\n")
        self.assertEqual(self.ok("cat-file", "-s", "v1"), f"{len(serialize_tag(tag))}\n")
        self.assertTrue(self.ok("cat-file", "-p", "v1").startswith(f"object {self.c2}\ntype commit\ntag v1\n"))
        # -m without -a also makes an annotated tag; -a without -m is refused
        self.ok("tag", "v2", "-m", "lightweight? no")
        self.assertEqual(self.repo.objects.read(self.repo.refs.resolve("refs/tags/v2"))[0], "tag")
        err = self.fail_cli("tag", "-a", "v3")
        self.assertIn("error: no tag message", err)
        self.assertIsNone(self.repo.refs.resolve("refs/tags/v3"))
        self.assertIn("error: no tag message", self.fail_cli("tag", "-a", "v3", "-m", "  "))
        v2_sha = self.repo.refs.resolve("refs/tags/v2")
        self.assertEqual(self.ok("tag", "-d", "v1", "v2"),
                         f"Deleted tag 'v1' (was {ref_sha[:7]})\nDeleted tag 'v2' (was {v2_sha[:7]})\n")
        self.assertEqual(self.ok("tag"), "")

    def test_exists_force_and_errors(self):
        self.ok("tag", "v1", "HEAD~1")
        # as git: fatal (exit 128)
        self.assertIn("fatal: tag 'v1' already exists", self.fail_cli("tag", "v1", code=128))
        self.assertIn("fatal: tag 'v1' already exists", self.fail_cli("tag", "-a", "v1", "-m", "x", code=128))
        self.assertEqual(self.repo.refs.resolve("refs/tags/v1"), self.c1)
        self.assertEqual(self.ok("tag", "-f", "v1"), f"Updated tag 'v1' (was {self.c1[:7]})\n")
        self.assertEqual(self.repo.refs.resolve("refs/tags/v1"), self.c2)
        self.assertEqual(self.ok("tag", "-f", "v1"), "")  # same value: nothing to say
        for bad in ("v 1", "a..b", "x.lock", "HEAD", "refs/tags/v1", "v1/", ".hidden", "a^b"):
            self.assertIn(f"fatal: '{bad}' is not a valid tag name", self.fail_cli("tag", bad, code=128), bad)
        self.assertIn("'-v1' is not a valid tag name", self.fail_cli("tag", "--", "-v1", code=128))
        self.assertIn("fatal: unknown revision 'nope'", self.fail_cli("tag", "v9", "nope", code=128))
        blob = self.ok("rev-parse", "HEAD:a.txt").strip()
        self.assertIn("is not a commit", self.fail_cli("tag", "v9", blob, code=128))
        self.assertIsNone(self.repo.refs.resolve("refs/tags/v9"))
        # usage errors
        self.assertEqual(self.cli("tag", "a", "b", "c")[0], 129)
        self.assertEqual(self.cli("tag", "-d")[0], 129)
        self.assertEqual(self.cli("tag", "-l", "-m", "x")[0], 129)
        self.assertEqual(self.cli("tag", "-n", "v5")[0], 129)

    def test_unborn_head(self):
        other = os.path.join(self.root, "empty")
        os.makedirs(other)
        ops.init(other)
        rc, _, err = self.cli("-C", other, "tag", "v1")
        self.assertEqual(rc, 128)
        self.assertIn("fatal: unknown revision 'HEAD'", err)
        self.assertIn("hint: the current branch 'main' has no commits yet", err)

    def test_peel_syntax_and_message_cleanup(self):
        self.ok("tag", "-a", "v1", "HEAD~1", "-m", "# dropped, as git tag does\nannotated  \n\n\nbody")
        tag_sha = self.repo.refs.resolve("refs/tags/v1")
        self.assertEqual(tags.read_tag(self.repo, tag_sha).message, "annotated\n\nbody\n")
        self.assertEqual(self.ok("rev-parse", "v1^{}").strip(), self.c1)
        self.assertEqual(self.ok("rev-parse", "v1^{commit}").strip(), self.c1)
        self.assertEqual(self.ok("rev-parse", "v1^{tag}").strip(), tag_sha)
        self.assertEqual(self.ok("cat-file", "-t", "v1^{tree}"), "tree\n")
        self.assertEqual(self.ok("log", "--oneline", "v1^{commit}"), f"{self.c1[:7]} (tag: v1) one\n")
        rc, _, err = self.cli("rev-parse", "HEAD^{blob}")
        self.assertEqual(rc, 128)
        self.assertIn("expected blob type", err)
        rc, _, err = self.cli("tag", "-a", "v2", "-m", "# only a comment")
        self.assertEqual((rc, "no tag message" in err), (1, True))

    def test_revparse_order_and_peeling(self):
        self.ok("tag", "-a", "v1", "HEAD~1", "-m", "annotated")
        self.ok("tag", "light", "HEAD")
        ops.branch_create(self.repo, "v1", self.c2)  # a branch with the same name: the tag wins
        tag_sha = self.repo.refs.resolve("refs/tags/v1")
        self.assertEqual(resolve_object(self.repo, "v1"), tag_sha)
        self.assertEqual(resolve_rev(self.repo, "v1"), self.c1)
        self.assertEqual(resolve_rev(self.repo, "refs/heads/v1"), self.c2)
        self.assertEqual(resolve_rev(self.repo, "refs/tags/v1"), self.c1)
        self.assertEqual(resolve_rev(self.repo, "light"), self.c2)
        self.assertEqual(resolve_rev(self.repo, "light~1"), self.c1)
        with self.assertRaises(RevisionError):
            resolve_rev(self.repo, "v1~1")  # c1 is the root
        self.assertEqual(resolve_object(self.repo, "v1:a.txt"), hash_object("blob", b"1\n"))
        self.assertEqual(self.ok("rev-parse", "v1").strip(), tag_sha)
        self.assertEqual(self.ok("rev-parse", "v1~0").strip(), self.c1)
        self.assertEqual(self.ok("rev-parse", "light").strip(), self.c2)
        # a tag of a tag peels through the chain
        self.ok("tag", "-a", "v1-again", "v1", "-m", "re-tag")
        again = tags.read_tag(self.repo, self.repo.refs.resolve("refs/tags/v1-again"))
        self.assertEqual((again.object, again.type), (tag_sha, "tag"))
        self.assertEqual(resolve_rev(self.repo, "v1-again"), self.c1)

    def test_commands_peel_annotated_tags(self):
        self.ok("tag", "-a", "v1", "HEAD~1", "-m", "annotated")
        self.assertIn("-1\n+2\n", self.ok("diff", "v1", "main"))
        self.assertEqual(self.ok("log", "--oneline", "v1"), f"{self.c1[:7]} (tag: v1) one\n")
        self.assertIn("HEAD is now at " + self.c1[:7], self.cli("switch", "--detach", "v1")[2])
        self.assertEqual(self.repo.head_commit(), self.c1)
        self.ok("switch", "main")
        self.assertIn("HEAD is now at " + self.c1[:7], self.cli("checkout", "v1")[2])
        self.ok("switch", "main")
        err = self.fail_cli("switch", "v1", code=128)
        self.assertIn("a branch is expected, got tag 'v1'", err)
        self.assertEqual(self.ok("branch"), "* main\n")
        self.assertEqual(self.ok("branch", "-a"), "* main\n")
        self.assertEqual(default_merge_message(self.repo, "v1"), "Merge tag 'v1'")
        ops.switch(self.repo, None, create="side", start="v1")
        c3 = self.commit_file("b.txt", "b\n", "side")
        self.ok("tag", "-a", "v2", "main", "-m", "two")
        self.ok("merge", "v2")
        head = self.repo.read_commit(self.repo.head_commit())
        self.assertEqual(head.message, "Merge tag 'v2'\n")
        self.assertEqual(set(head.parents), {c3, self.c2})
        self.assertEqual(self.ok("rev-parse", "v2:a.txt").strip(), hash_object("blob", b"2\n"))
        self.assertIn("", self.ok("grep", "-n", "2", "v2"))
        self.ok("reset", "--hard", "v1")
        self.assertEqual(self.repo.head_commit(), self.c1)

    def test_log_decorations_and_show(self):
        self.repo.refs.update("refs/remotes/origin/main", self.c2)
        ops.branch_create(self.repo, "feature", self.c2)
        self.ok("tag", "-a", "v1", "-m", "first release\n\nbody line")
        self.ok("tag", "v0", "HEAD~1")
        tag_sha = self.repo.refs.resolve("refs/tags/v1")
        self.assertEqual(self.ok("log", "--oneline"),
                         f"{self.c2[:7]} (HEAD -> main, tag: v1, origin/main, feature) two\n"
                         f"{self.c1[:7]} (tag: v0) one\n")
        decor = history.decorations(self.repo)
        self.assertEqual(decor[self.c2], ["HEAD -> main", "tag: v1", "origin/main", "feature"])
        self.assertNotIn(tag_sha, decor)  # the tag object itself is never decorated
        full = self.ok("log")
        self.assertTrue(full.startswith(f"commit {self.c2} (HEAD -> main, tag: v1, origin/main, feature)\n"))
        # --all starts from the peeled tag, not the tag object
        self.ok("switch", "--detach", "v0")
        entries = history.log_entries(self.repo, None, None, all_refs=True)
        self.assertEqual([s for s, _ in entries], [self.c2, self.c1])
        # show: the tag header, a blank line, then the commit as usual (HEAD is detached at v0 here)
        shown = self.ok("show", "v1")
        self.assertTrue(shown.startswith(
            "tag v1\nTagger: Test User <test@example.com>\n"
            f"Date:   {format_date(self.clock, 0)}\n\nfirst release\n\nbody line\n\n"
            f"commit {self.c2} (tag: v1, origin/main, feature, main)\nAuthor: Test User <test@example.com>\n"),
            shown)
        self.assertIn("\n-1\n+2\n", shown)
        self.assertEqual(self.ok("show", "v0"), self.ok("show", self.c1))
        self.assertEqual(self.ok("show", "v1:a.txt"), "2\n")

    def test_pack_connectivity_and_fsck_with_tags(self):
        self.ok("tag", "-a", "v1", "-m", "annotated")
        tag_sha = self.repo.refs.resolve("refs/tags/v1")
        store = self.repo.objects
        closure = pack.closure(store, [tag_sha])
        self.assertIn(tag_sha, closure)
        self.assertIn(self.c2, closure)
        self.assertIn(self.c1, closure)
        self.assertEqual(pack.objects_to_send(store, [tag_sha], [self.c2]), [tag_sha])
        self.assertEqual(pack.complete_commits(store, [tag_sha]), {self.c1, self.c2})
        self.assertEqual(pack.peel_tips(store, [tag_sha, self.c1]), ([self.c2, self.c1], {tag_sha}))
        pack.check_connectivity(store, tag_sha, set())
        self.assertEqual(pack.fsck(self.repo), [])
        # the pack format carries tag objects
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            other = ObjectStore(os.path.join(tmp, "objects"))
            data = pack.build_pack(store, pack.objects_to_send(store, [tag_sha], []))
            written = pack.unpack(other, data)
            self.assertIn(tag_sha, written)
            self.assertEqual(other.read(tag_sha)[0], "tag")
            pack.check_connectivity(other, tag_sha, set())
            # a tag whose commit is missing fails connectivity and fsck
            dangling = other.write("tag", serialize_tag(Tag("f" * 40, "commit", "d", None, "m\n")))
            with self.assertRaises(ObjectError):
                pack.check_connectivity(other, dangling, set())
        self.repo.refs.update("refs/tags/dangling", store.write(
            "tag", serialize_tag(Tag("f" * 40, "commit", "dangling", None, "m\n"))))
        problems = pack.fsck(self.repo)
        self.assertEqual(len(problems), 1)
        self.assertIn("refs/tags/dangling", problems[0])
        self.assertIn("missing object " + "f" * 40, problems[0])
        # tag_list still lists it (without a commit) rather than crashing
        info = [t for t in tags.tag_list(self.repo) if t.name == "dangling"][0]
        self.assertTrue(info.annotated)
        self.assertIsNone(info.commit)

    def test_library_functions(self):
        self.assertEqual(tags.tag_create(self.repo, "v1", self.c1), "")
        with self.assertRaises(MinigitError) as cm:
            tags.tag_create(self.repo, "v1")
        self.assertEqual(cm.exception.message, "tag 'v1' already exists")
        self.assertEqual(tags.tag_create(self.repo, "v1", force=True), f"Updated tag 'v1' (was {self.c1[:7]})")
        items = tags.tag_list(self.repo)
        self.assertEqual([(t.name, t.ref_sha, t.commit, t.annotated, t.subject) for t in items],
                         [("v1", self.c2, self.c2, False, "two")])
        self.assertEqual(tags.format_tag_list(items), "v1\n")
        self.assertEqual(tags.format_tag_list(items, with_message=True), "v1              two\n")
        self.assertEqual(tags.tag_delete(self.repo, "v1"), f"Deleted tag 'v1' (was {self.c2[:7]})")
        with self.assertRaises(MinigitError) as cm:
            tags.tag_delete(self.repo, "v1")
        self.assertEqual(cm.exception.message, "tag 'v1' not found.")
        with self.assertRaises(MinigitError):
            tags.tag_delete(self.repo, "a b")  # an invalid name is simply "not found"


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

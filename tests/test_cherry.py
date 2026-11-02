"""tests.test_cherry -- in-process unit tests for minigit/sequencer.py (cherry-pick / revert).

The CLI-level acceptance tests are in tests/test_cherry_pick_revert.py; these cover the engine's
edge cases (identity, file/dir clashes, bare repos, sequences of several commits).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from minigit import ops, status
from minigit import sequencer as cherry
from minigit.errors import MinigitError, NothingToCommit, UsageError
from minigit.index import read_index
from minigit.repo import Repository
from minigit.revparse import resolve_rev

ENV = {"MINIGIT_AUTHOR_NAME": "Unit Tester", "MINIGIT_AUTHOR_EMAIL": "unit@example.com",
       "MINIGIT_AUTHOR_DATE": "1700000000 +0000"}
OTHER = {"MINIGIT_AUTHOR_NAME": "Other Author", "MINIGIT_AUTHOR_EMAIL": "other@example.com",
         "MINIGIT_AUTHOR_DATE": "1600000000 +0100"}


class CherryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self._tmp.name, "w")
        home = os.path.join(self._tmp.name, "home")
        os.makedirs(home)
        env = dict(ENV, MINIGIT_GLOBAL_CONFIG=os.path.join(home, ".minigitconfig"), HOME=home, USERPROFILE=home)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo, _ = Repository.init(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # helpers ---------------------------------------------------------------
    def write(self, rel: str, data: bytes) -> None:
        p = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)

    def read(self, rel: str) -> bytes:
        with open(os.path.join(self.root, *rel.split("/")), "rb") as fh:
            return fh.read()

    def exists(self, rel: str) -> bool:
        return os.path.lexists(os.path.join(self.root, *rel.split("/")))

    def commit(self, msg: str, files: dict[str, bytes | None] | None = None, env: dict | None = None) -> str:
        for p, data in (files or {}).items():
            if data is None:
                ops.rm(self.repo, [p], self.root)
            else:
                self.write(p, data)
        ops.add(self.repo, ["."], self.root)
        with mock.patch.dict(os.environ, env or {}):
            sha, _ = ops.commit(self.repo, [msg])
        return sha

    def on_branch(self, name: str, start: str | None = None) -> None:
        if start is not None:
            ops.switch(self.repo, None, create=name, start=start)
        elif self.repo.refs.resolve("refs/heads/" + name) is None:
            ops.switch(self.repo, None, create=name)
        else:
            ops.switch(self.repo, name)

    def seed(self) -> str:
        return self.commit("base", {"f.txt": b"a\nb\nc\n", "other.txt": b"o\n"})

    def head(self) -> str:
        return self.repo.head_commit()


class MessageAndParentTests(CherryCase):
    def test_messages(self):
        base = self.seed()
        c = self.repo.read_commit(base)
        self.assertEqual(cherry.pick_label(base, c), f"{base[:7]} (base)")
        self.assertEqual(cherry.revert_message(c, base), f'Revert "base"\n\nThis reverts commit {base}.\n')
        self.assertEqual(cherry.cherry_pick_message(c, base, False), "base\n")
        self.assertEqual(cherry.cherry_pick_message(c, base, True),
                         f"base\n\n(cherry picked from commit {base})\n")

    def test_parent_for_root_and_mainline_errors(self):
        base = self.seed()
        c = self.repo.read_commit(base)
        self.assertIsNone(cherry.parent_for(self.repo, base, c, None, "cherry-pick"))
        with self.assertRaisesRegex(MinigitError, "mainline was specified"):
            cherry.parent_for(self.repo, base, c, 1, "cherry-pick")


class EngineEdgeCaseTests(CherryCase):
    def test_missing_identity_is_refused_before_anything_is_written(self):
        self.seed()
        self.on_branch("feat")
        feat = self.commit("feat", {"new.txt": b"n\n"})
        ops.switch(self.repo, "main")
        head = self.head()
        index_before = read_index(self.repo).flat()
        env = {k: v for k, v in os.environ.items() if k not in ("MINIGIT_AUTHOR_NAME", "MINIGIT_AUTHOR_EMAIL")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(MinigitError, "author identity unknown"):
                cherry.cherry_pick(self.repo, feat)
        self.assertFalse(self.exists("new.txt"))
        self.assertEqual(read_index(self.repo).flat(), index_before)
        self.assertEqual(self.head(), head)
        self.assertIsNone(self.repo.refs.read_special("ORIG_HEAD"))
        # -n needs no identity
        with mock.patch.dict(os.environ, env, clear=True):
            out = cherry.cherry_pick(self.repo, feat, no_commit=True)
        self.assertEqual(out.kind, "applied")
        self.assertEqual(self.read("new.txt"), b"n\n")

    def test_file_dir_clash_becomes_a_resolvable_conflict(self):
        # As in git: the directory wins the path and our file moves to 'd~HEAD' as an unmerged entry.
        self.seed()
        self.on_branch("feat")
        feat = self.commit("feat makes d a dir", {"d/x.txt": b"x\n", "f.txt": b"a\nb\nC\n"})
        ops.switch(self.repo, "main")
        self.commit("main makes d a file", {"d": b"file\n"})
        out = cherry.cherry_pick(self.repo, feat)
        self.assertEqual(out.kind, "conflict")
        self.assertIn("d~HEAD", out.conflicts)
        self.assertEqual(self.read("d/x.txt"), b"x\n")
        self.assertEqual(self.read("d~HEAD"), b"file\n")
        self.assertEqual(self.read("f.txt"), b"a\nb\nC\n")
        self.assertEqual(self.repo.cherry_pick_head(), feat)

    def test_bare_repo_is_refused(self):
        bare, _ = Repository.init(os.path.join(self._tmp.name, "bare.git"), bare=True)
        for fn in (lambda: cherry.cherry_pick(bare, "HEAD"), lambda: cherry.revert(bare, "HEAD"),
                   lambda: cherry.abort(bare, "cherry-pick"), lambda: cherry.continue_(bare, "revert")):
            with self.assertRaisesRegex(MinigitError, "must be run in a work tree"):
                fn()

    def test_unborn_head_is_refused(self):
        self.seed()
        self.repo.refs.set_symbolic_head("refs/heads/orphan")
        with self.assertRaisesRegex(MinigitError, "cannot revert on an unborn branch"):
            cherry.revert(self.repo, "main")

    def test_pick_keeps_author_and_revert_uses_identity(self):
        self.seed()
        self.on_branch("feat")
        feat = self.commit("feat", {"new.txt": b"n\n"}, env=OTHER)
        ops.switch(self.repo, "main")
        out = cherry.cherry_pick(self.repo, feat)
        self.assertEqual(out.kind, "committed")
        c = self.repo.read_commit(out.commit)
        self.assertEqual((c.author.name, c.author.timestamp), ("Other Author", 1600000000))
        self.assertEqual(c.committer.name, "Unit Tester")
        self.assertEqual(out.summary, f"[main {out.commit[:7]}] feat")
        out = cherry.revert(self.repo, "HEAD")
        c = self.repo.read_commit(out.commit)
        self.assertEqual(c.author.name, "Unit Tester")
        self.assertFalse(self.exists("new.txt"))

    def test_mainline_selects_parent(self):
        base = self.seed()
        self.on_branch("feat")
        feat = self.commit("feat", {"fs.txt": b"f\n"})
        ops.switch(self.repo, "main")
        self.commit("main", {"ms.txt": b"m\n"})
        from minigit import merge
        merge.merge(self.repo, "feat")
        m = self.head()
        self.on_branch("p", start=base)
        with self.assertRaisesRegex(MinigitError, "is a merge but no -m"):
            cherry.cherry_pick(self.repo, m)
        with self.assertRaisesRegex(MinigitError, "does not have parent 3"):
            cherry.cherry_pick(self.repo, m, mainline=3)
        cherry.cherry_pick(self.repo, m, mainline=2)
        self.assertTrue(self.exists("ms.txt"))
        self.assertFalse(self.exists("fs.txt"))
        self.assertNotEqual(feat, self.head())

    def test_empty_and_allow_empty(self):
        self.seed()
        head = self.head()
        with self.assertRaises(NothingToCommit):
            cherry.cherry_pick(self.repo, "HEAD")
        with self.assertRaisesRegex(NothingToCommit, "revert of .* produced no changes"):
            self.commit("two", {"f.txt": b"x\n"})
            cherry.revert(self.repo, "HEAD")
            cherry.revert(self.repo, "HEAD~1")
        self.assertNotEqual(self.head(), head)
        out = cherry.cherry_pick(self.repo, "HEAD", allow_empty=True)
        self.assertEqual(out.kind, "committed")
        self.assertEqual(self.repo.read_commit(out.commit).tree, self.repo.read_commit(out.old_head).tree)

    def test_no_commit_writes_merge_msg_but_no_state(self):
        self.seed()
        self.on_branch("feat")
        feat = self.commit("feat", {"new.txt": b"n\n"})
        ops.switch(self.repo, "main")
        out = cherry.cherry_pick(self.repo, feat, no_commit=True, record_origin=True)
        self.assertEqual(out.kind, "applied")
        self.assertIsNone(self.repo.cherry_pick_head())
        self.assertEqual(self.repo.read_state("MERGE_MSG").decode(),
                         f"feat\n\n(cherry picked from commit {feat})\n")


class ConflictStateTests(CherryCase):
    def conflict(self) -> tuple[str, str]:
        self.seed()
        self.on_branch("feat")
        feat = self.commit("feat", {"f.txt": b"a\nb\nFEAT\n"}, env=OTHER)
        ops.switch(self.repo, "main")
        main = self.commit("main", {"f.txt": b"a\nb\nMAIN\n"})
        out = cherry.cherry_pick(self.repo, feat)
        self.assertEqual(out.kind, "conflict")
        self.assertEqual(out.conflicts, ["f.txt"])
        self.assertEqual(out.error, f"could not apply {feat[:7]}... feat")
        return main, feat

    def test_state_revisions_status_and_commit(self):
        main, feat = self.conflict()
        self.assertEqual(self.repo.operation_in_progress(), ("cherry-pick", feat))
        self.assertEqual(resolve_rev(self.repo, "CHERRY_PICK_HEAD"), feat)
        long = status.format_status_long(status.compute_status(self.repo))
        self.assertIn(f"You are currently cherry-picking commit {feat[:7]}.", long)
        self.assertIn("fix conflicts and run", long)
        with self.assertRaisesRegex(MinigitError, "you have not concluded your cherry-pick"):
            from minigit import merge
            merge.merge(self.repo, "feat")
        with self.assertRaisesRegex(MinigitError, "cherry-pick in progress"):
            ops.switch(self.repo, "feat")
        with self.assertRaisesRegex(MinigitError, "soft reset in the middle of a cherry-pick"):
            ops.reset(self.repo, "HEAD", "soft")
        self.write("f.txt", b"a\nb\nBOTH\n")
        ops.add(self.repo, ["f.txt"], self.root)
        lines = cherry.continue_(self.repo, "cherry-pick")
        self.assertEqual(len([line for line in lines if line.startswith("[")]), 1)
        self.assertIn(" Author: Other Author <other@example.com>", lines)   # as git: author differs
        c = self.repo.read_commit(self.head())
        self.assertEqual(c.parents, (main,))
        self.assertEqual(c.author.name, "Other Author")
        self.assertEqual(c.message, "feat\n")
        self.assertIsNone(self.repo.operation_in_progress())
        self.assertIsNone(self.repo.read_state("MERGE_MSG"))

    def test_reset_mixed_clears_state(self):
        self.conflict()
        ops.reset(self.repo, "HEAD", "mixed")
        self.assertIsNone(self.repo.operation_in_progress())
        self.assertIsNone(self.repo.read_state("MERGE_MSG"))

    def test_wrong_action_abort_and_continue(self):
        self.conflict()
        with self.assertRaisesRegex(MinigitError, r"there is no revert to abort \(REVERT_HEAD missing\)"):
            cherry.abort(self.repo, "revert")
        with self.assertRaisesRegex(MinigitError, r"no revert in progress \(REVERT_HEAD missing\)"):
            cherry.continue_(self.repo, "revert")
        cherry.abort(self.repo, "cherry-pick")
        self.assertEqual(self.read("f.txt"), b"a\nb\nMAIN\n")


class SequenceTests(CherryCase):
    def feat_commits(self) -> list[str]:
        self.seed()
        self.on_branch("feat")
        c1 = self.commit("one", {"one.txt": b"1\n"})
        c2 = self.commit("two", {"f.txt": b"a\nb\nFEAT\n"})
        c3 = self.commit("three", {"three.txt": b"3\n"})
        ops.switch(self.repo, "main")
        return [c1, c2, c3]

    def test_several_commits_apply_in_order(self):
        c1, c2, c3 = self.feat_commits()
        start = self.head()
        out = cherry.start(self.repo, "cherry-pick", [c1, c2, c3], mainline=None, no_commit=False)
        self.assertEqual([o.kind for o in out], ["committed"] * 3)
        msgs = []
        sha = self.head()
        while sha != start:
            c = self.repo.read_commit(sha)
            msgs.append(c.message.strip())
            sha = c.parents[0]
        self.assertEqual(msgs, ["three", "two", "one"])
        self.assertEqual(self.repo.refs.read_special("ORIG_HEAD"), start)
        self.assertIsNone(self.repo.read_state(cherry.SEQUENCER_FILE))

    def test_several_with_no_commit_stack_into_the_index(self):
        c1, _c2, c3 = self.feat_commits()
        start = self.head()
        out = cherry.start(self.repo, "cherry-pick", [c1, c3], mainline=None, no_commit=True)
        self.assertEqual([o.kind for o in out], ["applied", "applied"])
        self.assertEqual(self.head(), start)
        self.assertEqual(sorted(read_index(self.repo).flat()), ["f.txt", "one.txt", "other.txt", "three.txt"])

    def test_ranges_expand_oldest_first(self):
        c1, c2, c3 = self.feat_commits()
        start = self.head()
        expand = lambda revs: cherry.expand_revs(self.repo, revs, "cherry-pick")  # noqa: E731
        self.assertEqual(expand(["main..feat"]), [c1, c2, c3])
        self.assertEqual(expand(["..feat"]), [c1, c2, c3])            # an empty side is HEAD
        self.assertEqual(expand(["feat~1..feat"]), [c3])
        self.assertEqual(expand(["feat~1^..feat"]), [c2, c3])         # A^..B includes A
        self.assertEqual(expand([c1, "feat~1..feat", "main"]), [c1, c3, start])   # mixed, in order
        self.assertEqual(expand([c1, "feat..main"]), [c1])                        # an empty range adds nothing
        out = cherry.start(self.repo, "cherry-pick", ["main..feat"], mainline=None, no_commit=False)
        self.assertEqual([o.kind for o in out], ["committed"] * 3)
        msgs, sha = [], self.head()
        while sha != start:
            c = self.repo.read_commit(sha)
            msgs.append(c.message.strip())
            sha = c.parents[0]
        self.assertEqual(msgs, ["three", "two", "one"])
        self.assertEqual(self.repo.refs.read_special("ORIG_HEAD"), start)

    def test_revert_range_undoes_oldest_first(self):
        self.feat_commits()
        cherry.start(self.repo, "cherry-pick", ["main..feat"], mainline=None, no_commit=False)
        top = self.head()
        out = cherry.start(self.repo, "revert", ["HEAD~2..HEAD"], mainline=None, no_commit=False)
        self.assertEqual([o.summary.split("] ", 1)[1] for o in out], ['Revert "two"', 'Revert "three"'])
        self.assertEqual(self.repo.read_commit(self.head()).parents[0], out[0].commit)
        self.assertEqual(self.repo.read_commit(out[0].commit).parents, (top,))
        self.assertEqual(sorted(self.repo.head_flat()), ["f.txt", "one.txt", "other.txt"])

    def test_range_with_a_merge_needs_mainline(self):
        from minigit import merge
        c1, c2, c3 = self.feat_commits()
        self.on_branch("side", c3)
        side = self.commit("side", {"side.txt": b"s\n"})
        ops.switch(self.repo, "feat")
        merge.merge(self.repo, "side", no_ff=True, message="merge side")
        merged = self.head()
        ops.switch(self.repo, "main")
        start = self.head()
        # the merge is in the range: refused only then, with git's wording, and nothing is applied
        with self.assertRaises(MinigitError) as cm:
            cherry.start(self.repo, "cherry-pick", ["main..feat"], mainline=None, no_commit=False)
        self.assertEqual(cm.exception.message, f"commit {merged[:7]} is a merge but no -m option was given.")
        self.assertEqual(self.head(), start)
        # a range without the merge needs no -m
        out = cherry.start(self.repo, "cherry-pick", [f"{c1}..{c3}"], mainline=None, no_commit=False)
        self.assertEqual([o.summary.split("] ", 1)[1] for o in out], ["two", "three"])
        # -m N picks the merge against its parent N
        out = cherry.start(self.repo, "cherry-pick", [f"{side}..feat"], mainline=1, no_commit=False)
        self.assertEqual([o.source for o in out], [merged])
        self.assertIn("side.txt", self.repo.head_flat())

    def test_empty_bad_and_symmetric_ranges_apply_nothing(self):
        self.feat_commits()
        start = self.head()
        with self.assertRaises(MinigitError) as cm:
            cherry.start(self.repo, "cherry-pick", ["feat..feat"], mainline=None, no_commit=False)
        self.assertEqual(cm.exception.message, "empty commit set passed")
        with self.assertRaisesRegex(MinigitError, "unknown revision 'nope'"):
            cherry.start(self.repo, "cherry-pick", ["nope..feat"], mainline=None, no_commit=False)
        with self.assertRaises(UsageError):
            cherry.start(self.repo, "cherry-pick", ["main...feat"], mainline=None, no_commit=False)
        self.assertEqual(self.head(), start)
        self.assertIsNone(self.repo.refs.read_special("ORIG_HEAD"))

    def test_conflict_in_the_middle_then_continue_finishes_the_rest(self):
        c1, c2, c3 = self.feat_commits()
        self.commit("main edits f", {"f.txt": b"a\nb\nMAIN\n"})
        out = cherry.start(self.repo, "cherry-pick", [c1, c2, c3], mainline=None, no_commit=False)
        self.assertEqual([o.kind for o in out], ["committed", "conflict"])
        self.assertTrue(self.exists("one.txt"))
        self.assertFalse(self.exists("three.txt"))
        self.assertEqual(self.repo.cherry_pick_head(), c2)
        self.write("f.txt", b"a\nb\nRESOLVED\n")
        ops.add(self.repo, ["f.txt"], self.root)
        lines = cherry.continue_(self.repo, "cherry-pick")
        self.assertEqual(len([line for line in lines if line.startswith("[")]), 2)
        self.assertTrue(self.exists("three.txt"))
        self.assertEqual(self.repo.read_commit(self.head()).message, "three\n")
        self.assertIsNone(self.repo.read_state(cherry.SEQUENCER_FILE))
        with self.assertRaisesRegex(MinigitError, "no cherry-pick in progress"):
            cherry.continue_(self.repo, "cherry-pick")

    def test_abort_in_the_middle_rewinds_to_the_start(self):
        c1, c2, c3 = self.feat_commits()
        start = self.commit("main edits f", {"f.txt": b"a\nb\nMAIN\n"})
        self.write("other.txt", b"dirty\n")
        cherry.start(self.repo, "cherry-pick", [c1, c2, c3], mainline=None, no_commit=False)
        cherry.abort(self.repo, "cherry-pick")
        self.assertEqual(self.head(), start)
        self.assertFalse(self.exists("one.txt"))
        self.assertEqual(self.read("f.txt"), b"a\nb\nMAIN\n")
        self.assertEqual(self.read("other.txt"), b"dirty\n")
        self.assertIsNone(self.repo.operation_in_progress())
        self.assertIsNone(self.repo.read_state(cherry.SEQUENCER_FILE))

    def test_abort_on_the_last_commit_also_rewinds_earlier_picks(self):
        """A conflict on the LAST commit of a sequence must still let --abort drop the picks before it."""
        c1, c2, c3 = self.feat_commits()
        start = self.commit("main edits f", {"f.txt": b"a\nb\nMAIN\n"})
        # c1 and c3 apply cleanly; c2 (which edits f.txt) conflicts as the LAST pick, nothing left to do
        outcomes = cherry.start(self.repo, "cherry-pick", [c1, c3, c2], mainline=None, no_commit=False)
        self.assertEqual(outcomes[-1].kind, "conflict")
        self.assertNotEqual(self.head(), start, "c1/c3 landed before the conflict")
        cherry.abort(self.repo, "cherry-pick")
        self.assertEqual(self.head(), start)
        self.assertFalse(self.exists("one.txt"))
        self.assertFalse(self.exists("three.txt"))
        self.assertEqual(self.read("f.txt"), b"a\nb\nMAIN\n")
        self.assertIsNone(self.repo.operation_in_progress())

    def test_bad_revision_anywhere_applies_nothing(self):
        c1, _c2, _c3 = self.feat_commits()
        start = self.head()
        with self.assertRaisesRegex(MinigitError, "unknown revision 'nope'"):
            cherry.start(self.repo, "cherry-pick", [c1, "nope"], mainline=None, no_commit=False)
        self.assertEqual(self.head(), start)
        with self.assertRaises(UsageError):
            cherry.start(self.repo, "revert", [], mainline=None, no_commit=False)


class CliSequenceTests(CherryCase):
    """The CLI handler for several revisions (one subprocess per command)."""

    def run_cli(self, *args: str):
        import subprocess
        import sys
        env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-m", "minigit", *args], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_cli_picks_several_then_continues_after_a_conflict(self):
        self.seed()
        self.on_branch("feat")
        c1 = self.commit("one", {"one.txt": b"1\n"})
        c2 = self.commit("two", {"f.txt": b"a\nb\nFEAT\n"})
        c3 = self.commit("three", {"three.txt": b"3\n"})
        ops.switch(self.repo, "main")
        self.commit("main edits f", {"f.txt": b"a\nb\nMAIN\n"})
        p = self.run_cli("cherry-pick", c1, c2, c3)
        self.assertEqual(p.returncode, 1, p)
        self.assertRegex(p.stdout, r"\[main [0-9a-f]{7}\] one\n Date: .*\n 1 file changed, 1 insertion\(\+\)\n"
                                   r" create mode 100644 one.txt\nAuto-merging f.txt\n"
                                   r"CONFLICT \(content\): Merge conflict in f.txt\n")
        self.assertIn(f"error: could not apply {c2[:7]}... two", p.stderr)
        self.write("f.txt", b"a\nb\nX\n")
        ops.add(self.repo, ["f.txt"], self.root)
        p = self.run_cli("cherry-pick", "--continue")
        self.assertEqual(p.returncode, 0, p)
        self.assertRegex(p.stdout, r"^\[main [0-9a-f]{7}\] two\n Date: .*\n 1 file changed, 1 insertion\(\+\), "
                                   r"1 deletion\(-\)\n\[main [0-9a-f]{7}\] three\n Date: .*\n"
                                   r" 1 file changed, 1 insertion\(\+\)\n create mode 100644 three.txt\n$")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

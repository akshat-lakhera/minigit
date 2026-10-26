"""Acceptance tests for ``minigit rebase`` (design: features/rebase.md).

They drive the real CLI (``python -m minigit``) in temporary repositories.
Run them from the project root with::

    python -m unittest tests.test_rebase -v
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest

from tests.helpers import ServerProcess, make_env, read, run, write

_ONELINE = re.compile(r"^[0-9a-f]{7}(?: \(.*?\))? (.*)$")


class _RebaseCase(unittest.TestCase):
    """One repository at ``self.repo`` (initial branch ``main``) driven through the CLI."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.env = make_env(self.root)
        self.repo = os.path.join(self.root, "repo")
        run(["init", self.repo], cwd=self.root, env=self.env)

    # -- CLI helpers ------------------------------------------------------
    def mg(self, *args, env=None):
        return run(list(args), cwd=self.repo, env=env or self.env)

    def fail(self, *args, code=1, env=None):
        res = run(list(args), cwd=self.repo, env=env or self.env, check=False)
        self.assertEqual(res.returncode, code, str(res))
        return res

    def put(self, rel, data):
        write(os.path.join(self.repo, *rel.split("/")), data)

    def get(self, rel) -> bytes:
        return read(os.path.join(self.repo, *rel.split("/")))

    def exists(self, rel) -> bool:
        return os.path.exists(os.path.join(self.repo, *rel.split("/")))

    def commit(self, message, files, env=None) -> str:
        for rel, data in files.items():
            self.put(rel, data)
        self.mg("add", ".", env=env)
        self.mg("commit", "-m", message, env=env)
        return self.sha("HEAD")

    def sha(self, rev) -> str:
        return self.mg("rev-parse", rev).out.strip()

    def subjects(self, rev="HEAD") -> list[str]:
        out = []
        for line in self.mg("log", "--oneline", rev).out.splitlines():
            m = _ONELINE.match(line)
            self.assertIsNotNone(m, line)
            out.append(m.group(1))
        return out

    def parent_count(self, rev) -> int:
        header = self.mg("cat-file", "-p", rev).out.split("\n\n", 1)[0]
        return sum(1 for line in header.splitlines() if line.startswith("parent "))

    def tree_of(self, rev) -> str:
        first = self.mg("cat-file", "-p", rev).out.splitlines()[0]
        self.assertTrue(first.startswith("tree "), first)
        return first[len("tree "):]

    def porcelain(self) -> str:
        return self.mg("status", "--porcelain").out

    def status(self) -> str:
        return self.mg("status").out

    def state_dir(self) -> str:
        return os.path.join(self.repo, ".minigit", "rebase")

    def assert_no_rebase(self):
        self.assertFalse(os.path.exists(self.state_dir()), "rebase state directory left behind")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".minigit", "REBASE_HEAD")))

    # -- histories --------------------------------------------------------
    def diverged(self):
        """base -> main: "main work" (m.txt); feature: "feature one" (f1.txt), "feature two" (f2.txt).

        Leaves HEAD on feature. Returns (main tip, feature tip)."""
        self.commit("base", {"a.txt": "a\n"})
        self.mg("branch", "feature")
        main_tip = self.commit("main work", {"m.txt": "main\n"})
        self.mg("switch", "feature")
        self.commit("feature one", {"f1.txt": "one\n"})
        tip = self.commit("feature two", {"f2.txt": "two\n"})
        return main_tip, tip

    def conflicting(self):
        """base a.txt = a/b/c; main changes line 3 one way, "feature edit" another way,
        then "feature extra" adds f.txt. Leaves HEAD on feature. Returns (main tip, edit, extra)."""
        self.commit("base", {"a.txt": "a\nb\nc\n"})
        self.mg("branch", "feature")
        main_tip = self.commit("main edit", {"a.txt": "a\nb\nC-main\n"})
        self.mg("switch", "feature")
        edit = self.commit("feature edit", {"a.txt": "a\nb\nC-feature\n"})
        extra = self.commit("feature extra", {"f.txt": "extra\n"})
        return main_tip, edit, extra


# ===========================================================================
# clean rebases
# ===========================================================================

class RebaseCleanTests(_RebaseCase):

    def test_linear_rebase_replays_commits_on_top_of_upstream(self):
        self.commit("base", {"a.txt": "a\n"})
        self.mg("branch", "feature")
        main_tip = self.commit("main work", {"m.txt": "main\n"})
        self.mg("switch", "feature")
        payload = {"f1.txt": b"one\n", "crlf.txt": b"x\r\ny\r\n", "bin.dat": b"\x00\x01\x02bin\xff"}
        self.commit("feature one", payload)
        old_tip = self.commit("feature two", {"f2.txt": "two\n"})

        res = self.mg("rebase", "main")
        self.assertIn("Successfully rebased and updated refs/heads/feature.", res.out)
        self.assertEqual(self.subjects(), ["feature two", "feature one", "main work", "base"])
        self.assertEqual(self.sha("HEAD~2"), main_tip)
        self.assertEqual(self.sha("feature"), self.sha("HEAD"))
        self.assertNotEqual(self.sha("HEAD"), old_tip)
        self.assertEqual(self.sha("main"), main_tip)
        self.assertEqual(self.sha("ORIG_HEAD"), old_tip)
        self.assertEqual(self.parent_count("HEAD"), 1)
        self.assertEqual(self.parent_count("HEAD~1"), 1)
        self.assertIn("On branch feature", self.status())
        self.assertEqual(self.porcelain(), "")
        for rel, data in payload.items():
            self.assertEqual(self.get(rel), data, rel)
        self.assertEqual(self.get("m.txt"), b"main\n")
        self.assert_no_rebase()

    def test_up_to_date_when_upstream_is_already_an_ancestor(self):
        self.commit("base", {"a.txt": "a\n"})
        self.mg("switch", "-c", "feature")
        tip = self.commit("feature one", {"f.txt": "f\n"})
        res = self.mg("rebase", "main")
        self.assertIn("Current branch feature is up to date.", res.out)
        self.assertEqual(self.sha("HEAD"), tip)
        self.assertIn("On branch feature", self.status())
        self.assert_no_rebase()

    def test_fast_forward_when_branch_has_no_own_commits(self):
        base = self.commit("base", {"a.txt": "a\n"})
        self.mg("branch", "feature")
        main_tip = self.commit("main work", {"m.txt": "m\n"})
        self.mg("switch", "feature")
        res = self.mg("rebase", "main")
        self.assertIn("Fast-forwarded feature to main.", res.out)
        self.assertEqual(self.sha("feature"), main_tip)
        self.assertEqual(self.sha("ORIG_HEAD"), base)
        self.assertEqual(self.get("m.txt"), b"m\n")
        self.assertIn("On branch feature", self.status())
        self.assertEqual(self.porcelain(), "")
        self.assert_no_rebase()

    def test_author_is_kept_and_committer_is_the_rebaser(self):
        alice = make_env(os.path.join(self.root, "alice"), user="alice")
        bob = make_env(os.path.join(self.root, "bob"), user="bob")
        bob["MINIGIT_AUTHOR_DATE"] = "1700009999 +0100"
        self.commit("base", {"a.txt": "a\n"})
        self.mg("branch", "feature")
        self.commit("main work", {"m.txt": "m\n"})
        self.mg("switch", "feature")
        self.commit("alice change", {"al.txt": "al\n"}, env=alice)
        self.mg("rebase", "main", env=bob)
        raw = self.mg("cat-file", "-p", "HEAD").out
        self.assertIn("\nauthor alice <alice@example.com> 1700000000 +0000\n", raw)
        self.assertIn("\ncommitter bob <bob@example.com> 1700009999 +0100\n", raw)
        self.assertTrue(raw.endswith("\n\nalice change\n"), raw)

    def test_commit_already_upstream_is_dropped(self):
        self.commit("base", {"a.txt": "a\nb\nc\n"})
        self.mg("branch", "feature")
        main_tip = self.commit("fix b on main", {"a.txt": "a\nB\nc\n"})
        self.mg("switch", "feature")
        dup = self.commit("fix b on feature", {"a.txt": "a\nB\nc\n"})
        self.commit("unique", {"u.txt": "u\n"})
        res = self.mg("rebase", "main")
        self.assertIn(f"dropping {dup[:7]} fix b on feature -- patch contents already upstream", res.out)
        self.assertIn("Successfully rebased and updated refs/heads/feature.", res.out)
        self.assertEqual(self.subjects(), ["unique", "fix b on main", "base"])
        self.assertEqual(self.sha("HEAD~1"), main_tip)

    def test_commit_that_was_empty_from_the_start_is_kept(self):
        self.commit("base", {"a.txt": "a\n"})
        self.mg("branch", "feature")
        self.commit("main work", {"m.txt": "m\n"})
        self.mg("switch", "feature")
        self.mg("commit", "--allow-empty", "-m", "empty marker")
        self.commit("feature one", {"f.txt": "f\n"})
        self.mg("rebase", "main")
        self.assertEqual(self.subjects(), ["feature one", "empty marker", "main work", "base"])
        self.assertEqual(self.tree_of("HEAD~1"), self.tree_of("HEAD~2"), "still an empty commit")

    def test_merge_commits_are_dropped_and_history_linearized(self):
        self.commit("base", {"a.txt": "a\n"})
        self.mg("branch", "feature")
        main_tip = self.commit("main work", {"m.txt": "m\n"})
        self.mg("switch", "feature")
        self.commit("f1", {"f1.txt": "1\n"})
        self.mg("branch", "side")
        self.commit("f2", {"f2.txt": "2\n"})
        self.mg("switch", "side")
        self.commit("s1", {"s1.txt": "s\n"})
        self.mg("switch", "feature")
        self.mg("merge", "--no-ff", "-m", "merge side", "side")
        self.assertEqual(self.parent_count("HEAD"), 2)

        self.mg("rebase", "main")
        subjects = self.subjects()
        self.assertNotIn("merge side", subjects)
        self.assertEqual(sorted(subjects[:3]), ["f1", "f2", "s1"])
        self.assertEqual(subjects[3:], ["main work", "base"])
        self.assertEqual(self.sha("HEAD~3"), main_tip)
        for i in range(3):
            self.assertEqual(self.parent_count(f"HEAD~{i}"), 1)
        for rel in ("a.txt", "m.txt", "f1.txt", "f2.txt", "s1.txt"):
            self.assertTrue(self.exists(rel), rel)
        self.assertEqual(self.porcelain(), "")

    def test_onto_replays_only_the_commits_after_upstream(self):
        self.commit("base", {"a.txt": "a\n"})
        self.mg("branch", "side")
        main_tip = self.commit("main work", {"m.txt": "m\n"})
        self.mg("switch", "side")
        side_tip = self.commit("side work", {"s.txt": "s\n"})
        self.mg("switch", "-c", "topic")
        self.commit("topic work", {"t.txt": "t\n"})

        res = self.mg("rebase", "--onto", "main", "side")
        self.assertIn("Successfully rebased and updated refs/heads/topic.", res.out)
        self.assertEqual(self.subjects(), ["topic work", "main work", "base"])
        self.assertEqual(self.sha("HEAD~1"), main_tip)
        self.assertEqual(self.sha("side"), side_tip)
        self.assertTrue(self.exists("t.txt"))
        self.assertTrue(self.exists("m.txt"))
        self.assertFalse(self.exists("s.txt"))
        self.assertEqual(self.porcelain(), "")

    def test_detached_head_is_rebased_without_moving_branches(self):
        main_tip, tip = self.diverged()
        self.mg("switch", "--detach", "feature")
        res = self.mg("rebase", "main")
        self.assertIn("Successfully rebased and updated detached HEAD.", res.out)
        self.assertIn("HEAD detached at", self.status())
        self.assertEqual(self.sha("HEAD~2"), main_tip)
        self.assertEqual(self.sha("feature"), tip)
        self.assertEqual(self.sha("ORIG_HEAD"), tip)
        self.assert_no_rebase()

    def test_default_upstream_comes_from_branch_config(self):
        main_tip, _tip = self.diverged()
        res = self.fail("rebase")
        self.assertIn("no upstream configured for branch 'feature'", res.err)
        self.assert_no_rebase()
        self.mg("config", "branch.feature.remote", ".")
        self.mg("config", "branch.feature.merge", "refs/heads/main")
        res = self.mg("rebase")
        self.assertIn("Successfully rebased and updated refs/heads/feature.", res.out)
        self.assertEqual(self.sha("HEAD~2"), main_tip)

    def test_unrelated_root_commit_is_replayed_onto_upstream(self):
        main_tip = self.commit("base", {"a.txt": "a\n"})
        # start an orphan branch: point HEAD at an unborn branch and empty the index
        write(os.path.join(self.repo, ".minigit", "HEAD"), b"ref: refs/heads/orphan\n")
        os.remove(os.path.join(self.repo, ".minigit", "index"))
        os.remove(os.path.join(self.repo, "a.txt"))
        self.commit("orphan root", {"o.txt": "o\n"})
        self.assertEqual(self.parent_count("HEAD"), 0)

        res = self.mg("rebase", "main")
        self.assertIn("Successfully rebased and updated refs/heads/orphan.", res.out)
        self.assertEqual(self.subjects(), ["orphan root", "base"])
        self.assertEqual(self.sha("HEAD~1"), main_tip)
        self.assertEqual(self.get("a.txt"), b"a\n")
        self.assertEqual(self.get("o.txt"), b"o\n")
        self.assertEqual(self.porcelain(), "")

    def test_replay_order_below_a_merge_is_git_topo_order(self):
        # A -- C1 -- S1 (side)                    dates: C1 < S1 < C2 < M < D < E
        #       \\-- C2 -- M -- D (topic)          M = merge of side into topic, parents (C2, S1)
        # A -- E (main)
        # Git orders the replay with a stack-driven topological sort (`--topo-order`): below the merge
        # the first-parent lineage (C2) goes before the second-parent lineage (S1), although S1 is
        # older. Committer-date order would replay C1, S1, C2, D instead.
        def at(ts):
            return dict(self.env, MINIGIT_AUTHOR_DATE=f"{ts} +0000")

        self.commit("A", {"a.txt": "A\n"}, env=at(1700000000))
        self.mg("switch", "-c", "topic")
        self.commit("C1", {"c1.txt": "C1\n"}, env=at(1700000200))
        self.mg("switch", "-c", "side")
        self.commit("S1", {"s1.txt": "S1\n"}, env=at(1700000300))
        self.mg("switch", "topic")
        self.commit("C2", {"c2.txt": "C2\n"}, env=at(1700000400))
        self.mg("merge", "--no-ff", "-m", "Merge side", "side", env=at(1700000500))
        self.commit("D", {"d.txt": "D\n"}, env=at(1700000600))
        self.mg("switch", "main")
        upstream = self.commit("E", {"e.txt": "E\n"}, env=at(1700000700))
        self.mg("switch", "topic")

        self.mg("rebase", "main")   # committer: Test User, 1700000000 +0000 (make_env)
        self.assertEqual(self.subjects(), ["D", "S1", "C2", "C1", "E", "A"])
        self.assertEqual(self.sha("HEAD~4"), upstream)
        for i in range(4):
            self.assertEqual(self.parent_count(f"HEAD~{i}"), 1)
        # The same history (same files, messages, author and committer dates) rebased by git 2.55 with
        # `git rebase main` ends in this commit, so the replayed ids are git's.
        self.assertEqual(self.sha("topic"), "b86088bbe3ee4584ad93500b4da6ec6a37828081")
        self.assertEqual(self.porcelain(), "")


# ===========================================================================
# conflicts: stop, --continue, --skip, --abort
# ===========================================================================

class RebaseConflictTests(_RebaseCase):

    def test_conflict_stops_then_continue_finishes(self):
        main_tip, edit, extra = self.conflicting()
        res = self.fail("rebase", "main")
        self.assertIn("CONFLICT (content): Merge conflict in a.txt", res.out)
        self.assertIn(f"Could not apply {edit[:7]}... feature edit", res.out)
        self.assertIn("minigit rebase --continue", res.out)
        self.assertEqual(self.get("a.txt"),
                         b"a\nb\n<<<<<<< HEAD\nC-main\n=======\nC-feature\n>>>>>>> "
                         + edit[:7].encode() + b" (feature edit)\n")
        self.assertEqual(self.porcelain(), "UU a.txt\n")
        st = self.status()
        self.assertIn(f"rebase in progress; onto {main_tip[:7]}", st)
        self.assertIn(f"You are currently rebasing branch 'feature' on '{main_tip[:7]}'.", st)
        self.assertIn('(fix conflicts and then run "minigit rebase --continue")', st)
        self.assertIn("Unmerged paths:", st)
        self.assertEqual(self.sha("REBASE_HEAD"), edit)
        self.assertEqual(self.sha("HEAD"), main_tip)
        self.assertEqual(self.sha("feature"), extra, "the branch must not move before the rebase ends")
        self.assertTrue(os.path.isdir(self.state_dir()))

        self.put("a.txt", "a\nb\nC-both\n")
        self.mg("add", "a.txt")
        self.assertIn('(all conflicts fixed: run "minigit rebase --continue")', self.status())
        res = self.mg("rebase", "--continue")
        self.assertIn("Successfully rebased and updated refs/heads/feature.", res.out)
        self.assertEqual(self.subjects(), ["feature extra", "feature edit", "main edit", "base"])
        self.assertEqual(self.sha("HEAD~2"), main_tip)
        self.assertEqual(self.sha("ORIG_HEAD"), extra)
        self.assertEqual(self.get("a.txt"), b"a\nb\nC-both\n")
        self.assertEqual(self.get("f.txt"), b"extra\n")
        self.assertIn("On branch feature", self.status())
        self.assertEqual(self.porcelain(), "")
        self.assert_no_rebase()
        self.assertIn("unknown revision 'REBASE_HEAD'", self.fail("rev-parse", "REBASE_HEAD").err)

    def test_continue_refuses_unresolved_paths_and_unstaged_changes(self):
        self.conflicting()
        self.fail("rebase", "main")
        res = self.fail("rebase", "--continue")
        self.assertIn("unresolved conflicts in: a.txt", res.err)
        self.assertTrue(os.path.isdir(self.state_dir()))

        self.put("a.txt", "a\nb\nC-both\n")
        self.mg("add", "a.txt")
        self.put("a.txt", "a\nb\nC-both, edited again\n")
        res = self.fail("rebase", "--continue")
        self.assertIn("unstaged changes", res.err)
        self.assertTrue(os.path.isdir(self.state_dir()))

        self.mg("add", "a.txt")
        self.mg("rebase", "--continue")
        self.assertEqual(self.get("a.txt"), b"a\nb\nC-both, edited again\n")
        self.assert_no_rebase()

    def test_continue_with_nothing_to_commit_points_to_skip(self):
        main_tip, _edit, _extra = self.conflicting()
        self.fail("rebase", "main")
        self.put("a.txt", "a\nb\nC-main\n")      # resolved to exactly what HEAD has
        self.mg("add", "a.txt")
        res = self.fail("rebase", "--continue")
        self.assertIn("no changes to commit", res.err)
        self.assertIn("minigit rebase --skip", res.err)
        self.assertTrue(os.path.isdir(self.state_dir()))

        res = self.mg("rebase", "--skip")
        self.assertIn("Successfully rebased and updated refs/heads/feature.", res.out)
        self.assertEqual(self.subjects(), ["feature extra", "main edit", "base"])
        self.assertEqual(self.sha("HEAD~1"), main_tip)

    def test_skip_drops_the_conflicting_commit(self):
        main_tip, _edit, extra = self.conflicting()
        self.fail("rebase", "main")
        res = self.mg("rebase", "--skip")
        self.assertIn("Successfully rebased and updated refs/heads/feature.", res.out)
        self.assertEqual(self.subjects(), ["feature extra", "main edit", "base"])
        self.assertEqual(self.get("a.txt"), b"a\nb\nC-main\n")
        self.assertEqual(self.get("f.txt"), b"extra\n")
        self.assertEqual(self.sha("ORIG_HEAD"), extra)
        self.assertEqual(self.porcelain(), "")
        self.assert_no_rebase()

    def test_abort_restores_the_original_branch_byte_for_byte(self):
        self.commit("base", {"a.txt": "a\nb\nc\n", "keep/k.txt": "k\n"})
        self.mg("branch", "feature")
        main_tip = self.commit("main edit", {"a.txt": "a\nb\nC-main\n", "m.txt": "m\n"})
        self.mg("switch", "feature")
        self.commit("feature one", {"f1.txt": "one\n"})
        tip = self.commit("feature edit", {"a.txt": "a\nb\nC-feature\n"})
        self.put("notes.txt", "untracked, keep me\n")

        res = self.fail("rebase", "main")
        self.assertIn(f"Could not apply {tip[:7]}... feature edit", res.out)
        self.assertEqual(self.subjects()[:2], ["feature one", "main edit"], "first pick was applied")
        self.assertTrue(self.exists("m.txt"))

        res = self.mg("rebase", "--abort")
        self.assertEqual(res.out, "")
        self.assertEqual(self.sha("HEAD"), tip)
        self.assertEqual(self.sha("feature"), tip)
        self.assertEqual(self.sha("main"), main_tip)
        self.assertIn("On branch feature", self.status())
        self.assertEqual(self.get("a.txt"), b"a\nb\nC-feature\n")
        self.assertEqual(self.get("f1.txt"), b"one\n")
        self.assertEqual(self.get("keep/k.txt"), b"k\n")
        self.assertFalse(self.exists("m.txt"))
        self.assertEqual(self.get("notes.txt"), b"untracked, keep me\n")
        self.assertEqual(self.porcelain(), "?? notes.txt\n")
        self.assert_no_rebase()

    def test_manual_commit_during_the_stop_is_kept_by_continue(self):
        main_tip, _edit, _extra = self.conflicting()
        self.fail("rebase", "main")
        self.put("a.txt", "a\nb\nC-both\n")
        self.mg("add", "a.txt")
        self.mg("commit", "-m", "manual resolution")
        res = self.mg("rebase", "--continue")
        self.assertIn("Successfully rebased and updated refs/heads/feature.", res.out)
        self.assertEqual(self.subjects(), ["feature extra", "manual resolution", "main edit", "base"])
        self.assertEqual(self.sha("HEAD~2"), main_tip)
        self.assert_no_rebase()

    def test_modify_delete_conflict_resolved_by_removing(self):
        self.commit("base", {"a.txt": "a\nb\nc\n", "b.txt": "b\n"})
        self.mg("branch", "feature")
        self.commit("main edit", {"a.txt": "a\nb\nC-main\n"})
        self.mg("switch", "feature")
        self.mg("rm", "a.txt")
        self.mg("commit", "-m", "delete a")
        dele = self.sha("HEAD")

        res = self.fail("rebase", "main")
        self.assertIn(f"CONFLICT (modify/delete): a.txt deleted in {dele[:7]} (delete a) "
                      "and modified in HEAD.", res.out)
        self.assertEqual(self.porcelain(), "UD a.txt\n")
        self.assertEqual(self.get("a.txt"), b"a\nb\nC-main\n")
        self.mg("rm", "-f", "a.txt")
        self.mg("rebase", "--continue")
        self.assertFalse(self.exists("a.txt"))
        self.assertEqual(self.subjects(), ["delete a", "main edit", "base"])
        self.assertEqual(self.porcelain(), "")

    def test_branch_switching_commands_are_blocked_while_rebasing(self):
        _main_tip, edit, extra = self.conflicting()
        self.fail("rebase", "main")
        for args in (["switch", "main"], ["checkout", "main"], ["switch", "-c", "other"],
                     ["merge", "main"], ["rebase", "main"]):
            res = self.fail(*args)
            self.assertIn("rebase in progress", res.err, args)
        res = self.fail("branch", "-D", "feature")
        self.assertIn("being rebased", res.err)
        self.assertEqual(self.sha("feature"), extra)
        # path-level tools keep working: take the picked commit's version of the file
        self.mg("checkout", "REBASE_HEAD", "--", "a.txt")
        self.assertEqual(self.get("a.txt"), b"a\nb\nC-feature\n")
        self.assertEqual(self.sha("REBASE_HEAD"), edit)
        self.mg("rebase", "--continue")
        self.assertEqual(self.get("a.txt"), b"a\nb\nC-feature\n")
        self.assert_no_rebase()

    def test_refusals_name_the_rebase_even_after_a_manual_commit(self):
        main_tip, _edit, _extra = self.conflicting()
        self.fail("rebase", "main")
        expected = "rebase in progress; run 'minigit rebase --continue', '--skip' or '--abort' first"
        self.assertIn("cannot stash: " + expected, self.fail("stash").err)
        self.put("a.txt", "a\nb\nresolved\n")
        self.mg("add", "a.txt")
        self.mg("commit", "-m", "resolved by hand")
        self.assertFalse(self.exists(".minigit/REBASE_HEAD"))   # the commit cleared it; the rebase goes on
        for args in (["stash"], ["switch", "main"], ["checkout", "main"],
                     ["merge", "main"], ["cherry-pick", main_tip], ["revert", main_tip]):
            res = self.fail(*args)
            self.assertIn(expected, res.err, args)
            self.assertNotIn("REBASE_HEAD", res.err, args)
            self.assertNotIn("'minigit commit'", res.err, args)
        self.mg("rebase", "--continue")
        self.assert_no_rebase()

    def test_untracked_file_blocking_a_later_pick_stops_and_continue_retries(self):
        self.commit("base", {"a.txt": "a\nb\nc\n"})
        self.mg("branch", "feature")
        self.commit("main edit", {"a.txt": "a\nb\nC-main\n"})
        self.mg("switch", "feature")
        self.commit("feature edit", {"a.txt": "a\nb\nC-feature\n"})
        self.commit("feature adds x", {"x.txt": "from feature\n"})
        self.fail("rebase", "main")
        self.put("a.txt", "a\nb\nC-both\n")
        self.mg("add", "a.txt")
        self.put("x.txt", "mine, untracked\n")

        res = self.fail("rebase", "--continue")
        self.assertIn("would be overwritten by rebase", res.err)
        self.assertIn("x.txt", res.err)
        self.assertEqual(self.get("x.txt"), b"mine, untracked\n")
        self.assertTrue(os.path.isdir(self.state_dir()))
        self.assertEqual(self.subjects()[0], "feature edit", "the resolved commit is already recorded")

        os.remove(os.path.join(self.repo, "x.txt"))
        res = self.mg("rebase", "--continue")
        self.assertIn("Successfully rebased and updated refs/heads/feature.", res.out)
        self.assertEqual(self.subjects(), ["feature adds x", "feature edit", "main edit", "base"])
        self.assertEqual(self.get("x.txt"), b"from feature\n")
        self.assert_no_rebase()


# ===========================================================================
# refusals and usage
# ===========================================================================

class RebaseRefusalTests(_RebaseCase):

    def test_refuses_unstaged_and_staged_changes(self):
        _main_tip, tip = self.diverged()
        self.put("a.txt", "dirty\n")
        res = self.fail("rebase", "main")
        self.assertIn("cannot rebase: you have unstaged changes", res.err)
        self.mg("add", "a.txt")
        res = self.fail("rebase", "main")
        self.assertIn("cannot rebase: your index contains uncommitted changes", res.err)
        self.assertEqual(self.get("a.txt"), b"dirty\n")
        self.assertEqual(self.sha("HEAD"), tip)
        self.assertIn("On branch feature", self.status())
        self.assert_no_rebase()

    def test_refuses_untracked_file_in_the_way_before_touching_anything(self):
        self.commit("base", {"a.txt": "a\n"})
        self.mg("branch", "feature")
        self.commit("main adds new", {"new.txt": "main\n"})
        self.mg("switch", "feature")
        tip = self.commit("feature one", {"f.txt": "f\n"})
        self.put("new.txt", "mine\n")
        res = self.fail("rebase", "main")
        self.assertIn("error: The following untracked working tree files would be overwritten by rebase:\n"
                      "\tnew.txt\nPlease move or remove them before you rebase.", res.err)
        self.assertNotIn("local changes", res.err)
        self.assertEqual(self.get("new.txt"), b"mine\n")
        self.assertEqual(self.sha("HEAD"), tip)
        self.assertIn("On branch feature", self.status())
        self.assertEqual(self.porcelain(), "?? new.txt\n")
        self.assert_no_rebase()

    def test_refuses_unborn_head_and_open_merge(self):
        res = self.fail("rebase", "main")
        self.assertIn("does not have any commits yet", res.err)
        self.commit("base", {"a.txt": "a\nb\nc\n"})
        self.mg("branch", "other")
        self.commit("main edit", {"a.txt": "a\nb\nC-main\n"})
        self.mg("switch", "other")
        self.commit("other edit", {"a.txt": "a\nb\nC-other\n"})
        self.fail("merge", "main")
        res = self.fail("rebase", "main")
        self.assertIn("you have not concluded your merge", res.err)
        self.assert_no_rebase()

    def test_continue_skip_abort_without_a_rebase(self):
        self.commit("base", {"a.txt": "a\n"})
        for flag in ("--continue", "--skip", "--abort"):
            res = self.fail("rebase", flag)
            self.assertIn("no rebase in progress", res.err, flag)

    def test_usage_errors(self):
        self.diverged()
        self.fail("rebase", "--continue", "main", code=2)
        self.fail("rebase", "--skip", "--abort", code=2)
        self.fail("rebase", "--abort", "--onto", "main", code=2)
        res = self.fail("rebase", "nosuch")
        self.assertIn("unknown revision 'nosuch'", res.err)
        self.assert_no_rebase()
        helptext = self.mg("rebase", "--help").text
        for flag in ("--onto", "--continue", "--skip", "--abort"):
            self.assertIn(flag, helptext)
        self.assertIn("rebase", self.mg("--help").text)


# ===========================================================================
# collaboration: fetch + rebase + push instead of a merge commit
# ===========================================================================

class RebaseCollaborationTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        admin = make_env(self.root)
        self.srv_root = os.path.join(self.root, "srv")
        run(["server", "init", self.srv_root], cwd=self.root, env=admin)
        self.tokens = {u: run(["server", "add-user", self.srv_root, u], cwd=self.root, env=admin).out.strip()
                       for u in ("alice", "bob")}
        run(["server", "create-repo", self.srv_root, "proj"], cwd=self.root, env=admin)
        self.server = ServerProcess(self.srv_root, env=admin)
        self.server.start()
        self.addCleanup(self.server.stop)       # runs before the temp dir cleanup

    def user(self, name):
        env = make_env(os.path.join(self.root, "env-" + name), user=name, token=self.tokens[name])
        dest = os.path.join(self.root, name)
        run(["clone", f"{self.server.url}/proj", dest], cwd=self.root, env=env)
        return lambda *args, check=True: run(list(args), cwd=dest, env=env, check=check), dest

    def test_fetch_rebase_push_keeps_history_linear(self):
        alice, alice_dir = self.user("alice")
        write(os.path.join(alice_dir, "a.txt"), "a\n")
        alice("add", ".")
        alice("commit", "-m", "base")
        alice("push", "-u")
        bob, bob_dir = self.user("bob")

        write(os.path.join(alice_dir, "alice.txt"), "alice\n")
        alice("add", ".")
        alice("commit", "-m", "alice work")
        alice("push")
        write(os.path.join(bob_dir, "bob.txt"), "bob\n")
        bob("add", ".")
        bob("commit", "-m", "bob work")
        self.assertEqual(bob("push", check=False).returncode, 1)

        bob("fetch")
        res = bob("rebase")                      # default upstream: origin/main from the clone
        self.assertIn("Successfully rebased and updated refs/heads/main.", res.out)
        bob("push")
        log = bob("log", "--oneline").out.splitlines()
        self.assertEqual([_ONELINE.match(line).group(1) for line in log], ["bob work", "alice work", "base"])
        self.assertNotIn("Merge", bob("log").out)

        alice("pull")
        self.assertEqual(alice("rev-parse", "HEAD").out, bob("rev-parse", "HEAD").out)
        self.assertEqual(read(os.path.join(alice_dir, "bob.txt")), b"bob\n")

    def test_pull_rebase_end_to_end(self):
        alice, alice_dir = self.user("alice")
        write(os.path.join(alice_dir, "a.txt"), "a\n")
        alice("add", ".")
        alice("commit", "-m", "base")
        alice("push", "-u")
        bob, bob_dir = self.user("bob")

        # nothing to do
        res = bob("pull", "--rebase")
        self.assertEqual(res.out, "Already up to date.\n")
        self.assertEqual(bob("branch", "-vv").out, f"* main {bob('rev-parse', '--short', 'HEAD').out.strip()} "
                                                  "[origin/main] base\n")

        # bob's own commit is replayed on top of alice's: linear history, no merge commit
        write(os.path.join(alice_dir, "alice.txt"), "alice\n")
        alice("add", ".")
        alice("commit", "-m", "alice work")
        alice("push")
        write(os.path.join(bob_dir, "bob.txt"), "bob\n")
        bob("add", ".")
        bob("commit", "-m", "bob work")
        bob("fetch")
        self.assertIn("[origin/main: ahead 1, behind 1] bob work", bob("branch", "-vv").out)
        res = bob("pull", "--rebase")
        self.assertEqual(res.out.splitlines()[-1], "Successfully rebased and updated refs/heads/main.")
        log = bob("log", "--oneline").out.splitlines()
        self.assertEqual([_ONELINE.match(line).group(1) for line in log], ["bob work", "alice work", "base"])
        self.assertNotIn("Merge", bob("log").out)
        self.assertIn("[origin/main: ahead 1] bob work", bob("branch", "-vv").out)
        self.assertEqual(bob("status").out.splitlines()[1], "Your branch is ahead of 'origin/main' by 1 commit.")
        bob("push")

        # no own commits: a fast-forward
        res = alice("pull", "--rebase")
        self.assertEqual(res.out.splitlines()[-1], "Fast-forwarded main to origin/main.")
        self.assertEqual(alice("rev-parse", "HEAD").out, bob("rev-parse", "HEAD").out)
        self.assertEqual(read(os.path.join(alice_dir, "bob.txt")), b"bob\n")

        # a dirty tree is refused with rebase's message, before anything is fetched
        write(os.path.join(alice_dir, "a.txt"), "alice\n")
        alice("commit", "-a", "-m", "alice a")
        alice("push")
        write(os.path.join(bob_dir, "scratch.txt"), "s\n")
        bob("add", "scratch.txt")
        res = bob("pull", "--rebase", check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("error: cannot rebase: your index contains uncommitted changes", res.err)
        self.assertEqual(bob("rev-parse", "origin/main").out, bob("rev-parse", "HEAD").out)   # no fetch happened
        bob("rm", "--cached", "scratch.txt")
        os.unlink(os.path.join(bob_dir, "scratch.txt"))

        # a conflict stops like 'rebase' does; status reports it; --continue finishes it
        write(os.path.join(bob_dir, "a.txt"), "bob\n")
        bob("commit", "-a", "-m", "bob a")
        res = bob("pull", "--rebase", check=False)
        self.assertEqual(res.returncode, 1, str(res))
        self.assertIn("main -> origin/main", res.out)
        self.assertIn("CONFLICT (content): Merge conflict in a.txt\n", res.out)
        self.assertIn("Could not apply", res.out)
        self.assertIn('then run "minigit rebase --continue".', res.out)
        status = bob("status").out
        self.assertIn("You are currently rebasing branch 'main' on", status)
        self.assertIn("\tboth modified:   a.txt", status)
        self.assertIn(b"<<<<<<< HEAD\nalice\n=======\nbob\n>>>>>>> ", read(os.path.join(bob_dir, "a.txt")))
        res = bob("pull", "--rebase", check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("error: rebase in progress; finish it first", res.err)
        write(os.path.join(bob_dir, "a.txt"), "both\n")
        bob("add", "a.txt")
        res = bob("rebase", "--continue")
        self.assertEqual(res.out.splitlines()[-1], "Successfully rebased and updated refs/heads/main.")
        self.assertEqual(bob("status").out.splitlines()[1], "Your branch is ahead of 'origin/main' by 1 commit.")
        bob("push")
        log = bob("log", "--oneline").out.splitlines()
        self.assertEqual([_ONELINE.match(line).group(1) for line in log],
                         ["bob a", "alice a", "bob work", "alice work", "base"])

        # --no-rebase (the default) still merges
        write(os.path.join(alice_dir, "alice2.txt"), "2\n")
        alice("add", ".")
        alice("commit", "-m", "alice two")
        res = alice("pull", "--no-rebase")
        self.assertIn("Merge made by the 'three-way' strategy.", res.out)
        self.assertEqual(alice("cat-file", "-p", "HEAD").out.count("parent "), 2)

    def test_status_and_branch_report_a_gone_upstream(self):
        alice, alice_dir = self.user("alice")
        write(os.path.join(alice_dir, "a.txt"), "a\n")
        alice("add", ".")
        alice("commit", "-m", "base")
        alice("push", "-u")
        alice("push", "origin", "main:feature")
        bob, bob_dir = self.user("bob")
        bob("switch", "feature")
        alice("push", "--delete", "origin", "feature")
        bob("fetch")
        self.assertEqual(bob("status").out,
                         "On branch feature\n"
                         "Your branch is based on 'origin/feature', but the upstream is gone.\n"
                         '  (use "minigit branch --unset-upstream" to fixup)\n'
                         "\nnothing to commit, working tree clean\n")
        self.assertEqual(bob("status", "--porcelain").out, "")
        sha = bob("rev-parse", "--short", "HEAD").out.strip()
        self.assertEqual(bob("branch", "-vv").out,
                         f"* feature {sha} [origin/feature: gone] base\n  main    {sha} [origin/main] base\n")
        res = bob("branch", "-u", "origin/nosuch", check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("error: the requested upstream branch 'origin/nosuch' does not exist", res.err)
        self.assertEqual(bob("branch", "--unset-upstream").out, "")
        self.assertEqual(bob("status").out, "On branch feature\nnothing to commit, working tree clean\n")
        res = bob("branch", "--unset-upstream", check=False)
        self.assertEqual(res.returncode, 1)
        self.assertIn("error: branch 'feature' has no upstream information", res.err)
        self.assertEqual(bob("branch", "-u", "origin/main").out, "branch 'feature' set up to track 'origin/main'.\n")
        self.assertEqual(bob("config", "branch.feature.merge").out.strip(), "refs/heads/main")
        self.assertEqual(bob("branch", "-vv").out,
                         f"* feature {sha} [origin/main] base\n  main    {sha} [origin/main] base\n")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

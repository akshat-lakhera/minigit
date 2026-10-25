"""tests.test_merge -- owner: M4 merge."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from unittest import mock

from minigit.merge3 import Merge3Result, merge_blobs, merge_lines

try:  # the tree/command tests need M1 + M2; merge3 tests above run regardless
    from minigit import merge as M
    from minigit.errors import CheckoutConflictError, MinigitError
    from minigit.index import locked_index, read_index
    from minigit.objects import Commit, parse_commit, serialize_commit
    from minigit.repo import Repository
    from minigit.trees import build_tree, commit_flat
    from minigit.worktree import checkout_tree, stage_file
    _IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover
    _IMPORT_ERROR = exc

FILE, EXE = "100644", "100755"


def L(text: str) -> list[bytes]:
    data = text.encode()
    if not data:
        return []
    parts = data.split(b"\n")
    out = [p + b"\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


# ---------------------------------------------------------------------------
# merge3
# ---------------------------------------------------------------------------

class Merge3Tests(unittest.TestCase):
    def test_only_ours_changed(self):
        r = merge_lines(L("a\nb\nc\n"), L("a\nB\nc\n"), L("a\nb\nc\n"))
        self.assertEqual(r, Merge3Result(L("a\nB\nc\n"), 0))

    def test_only_theirs_changed(self):
        r = merge_lines(L("a\nb\nc\n"), L("a\nb\nc\n"), L("a\nb\nC\n"))
        self.assertEqual(b"".join(r.lines), b"a\nb\nC\n")
        self.assertEqual(r.conflicts, 0)

    def test_both_same_change(self):
        r = merge_lines(L("a\nb\nc\n"), L("a\nX\nc\n"), L("a\nX\nc\n"))
        self.assertEqual((b"".join(r.lines), r.conflicts), (b"a\nX\nc\n", 0))

    def test_non_overlapping_changes_clean(self):
        base = "1\n2\n3\n4\n5\n6\n7\n"
        r = merge_lines(L(base), L("ONE\n2\n3\n4\n5\n6\n7\n"), L("1\n2\n3\n4\n5\n6\nSEVEN\n"))
        self.assertEqual(b"".join(r.lines), b"ONE\n2\n3\n4\n5\n6\nSEVEN\n")
        self.assertEqual(r.conflicts, 0)

    def test_conflict_markers_exact(self):
        r = merge_lines(L("a\nb\nc\n"), L("a\nours\nc\n"), L("a\ntheirs\nc\n"), "HEAD", "feature")
        self.assertEqual(r.conflicts, 1)
        self.assertEqual(b"".join(r.lines),
                         b"a\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\nc\n")

    def test_multiple_conflicts_counted(self):
        base = "a\nb\nc\nd\ne\n"
        r = merge_lines(L(base), L("A1\nb\nc\nd\nE1\n"), L("A2\nb\nc\nd\nE2\n"))
        self.assertEqual(r.conflicts, 2)
        self.assertEqual(b"".join(r.lines).count(b"<<<<<<< HEAD\n"), 2)

    def test_adjacent_edits_conflict(self):
        r = merge_lines(L("a\nb\nc\nd\n"), L("a\nB\nc\nd\n"), L("a\nb\nC\nd\n"))
        self.assertEqual(r.conflicts, 1)
        self.assertEqual(b"".join(r.lines),
                         b"a\n<<<<<<< HEAD\nB\nc\n=======\nb\nC\n>>>>>>> theirs\nd\n")

    def test_deletion_one_side(self):
        r = merge_lines(L("a\nb\nc\nd\ne\n"), L("a\nc\nd\ne\n"), L("a\nb\nc\nd\nE\n"))
        self.assertEqual((b"".join(r.lines), r.conflicts), (b"a\nc\nd\nE\n", 0))

    def test_same_deletion_both_sides(self):
        r = merge_lines(L("a\nb\nc\n"), L("a\nc\n"), L("a\nc\n"))
        self.assertEqual((b"".join(r.lines), r.conflicts), (b"a\nc\n", 0))

    def test_delete_vs_modify_conflicts(self):
        r = merge_lines(L("a\nb\nc\n"), L("a\nc\n"), L("a\nB\nc\n"))
        self.assertEqual(r.conflicts, 1)
        self.assertEqual(b"".join(r.lines), b"a\n<<<<<<< HEAD\n=======\nB\n>>>>>>> theirs\nc\n")

    def test_insertions_at_same_point(self):
        same = merge_lines(L("a\nb\n"), L("a\nnew\nb\n"), L("a\nnew\nb\n"))
        self.assertEqual((b"".join(same.lines), same.conflicts), (b"a\nnew\nb\n", 0))
        diff = merge_lines(L("a\nb\n"), L("a\nx\nb\n"), L("a\ny\nb\n"))
        self.assertEqual(diff.conflicts, 1)

    def test_append_at_end_both_sides(self):
        r = merge_lines(L("a\n"), L("a\nx\n"), L("a\ny\n"))
        self.assertEqual(b"".join(r.lines), b"a\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> theirs\n")

    def test_add_add_empty_base(self):
        content, clean = merge_blobs(None, b"one\ntwo\n", b"uno\ndos\n", "HEAD", "other")
        self.assertFalse(clean)
        self.assertEqual(content, b"<<<<<<< HEAD\none\ntwo\n=======\nuno\ndos\n>>>>>>> other\n")

    def test_add_add_identical_is_clean(self):
        self.assertEqual(merge_blobs(None, b"same\n", b"same\n"), (b"same\n", True))

    def test_empty_bytes_base(self):
        content, clean = merge_blobs(b"", b"x\n", b"y\n")
        self.assertFalse(clean)
        self.assertTrue(content.startswith(b"<<<<<<< HEAD\nx\n"))

    def test_blob_shortcuts(self):
        self.assertEqual(merge_blobs(b"b\n", b"b\n", b"t\n"), (b"t\n", True))
        self.assertEqual(merge_blobs(b"b\n", b"o\n", b"b\n"), (b"o\n", True))

    def test_no_final_newline_conflict_gets_newline_before_markers(self):
        content, clean = merge_blobs(b"a\nb", b"a\nours", b"a\ntheirs")
        self.assertFalse(clean)
        self.assertEqual(content, b"a\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> theirs\n")

    def test_no_final_newline_preserved_when_clean(self):
        content, clean = merge_blobs(b"a\nb\nc\nd", b"A\nb\nc\nd", b"a\nb\nc\nD")
        self.assertTrue(clean)
        self.assertEqual(content, b"A\nb\nc\nD")
        content, clean = merge_blobs(b"a\nb\nc\nd\n", b"A\nb\nc\nd\n", b"a\nb\nc\nd")
        self.assertEqual((content, clean), (b"A\nb\nc\nd", True))

    def test_crlf_markers(self):
        content, clean = merge_blobs(b"a\r\nb\r\nc\r\n", b"a\r\nX\r\nc\r\n", b"a\r\nY\r\nc\r\n",
                                     "HEAD", "br")
        self.assertFalse(clean)
        self.assertEqual(content, b"a\r\n<<<<<<< HEAD\r\nX\r\n=======\r\nY\r\n>>>>>>> br\r\nc\r\n")

    def test_crlf_preserved_on_clean_merge(self):
        content, clean = merge_blobs(b"a\r\nb\r\nc\r\nd\r\n", b"A\r\nb\r\nc\r\nd\r\n",
                                     b"a\r\nb\r\nc\r\nD\r\n")
        self.assertEqual((content, clean), (b"A\r\nb\r\nc\r\nD\r\n", True))

    def test_lf_markers_when_ours_lf(self):
        content, _ = merge_blobs(b"a\nb\n", b"a\nX\n", b"a\r\nY\r\n")
        self.assertIn(b"=======\n", content)
        self.assertNotIn(b"=======\r\n", content)

    def test_empty_ours_and_theirs(self):
        self.assertEqual(merge_blobs(b"a\n", b"", b""), (b"", True))
        self.assertEqual(merge_lines([], [], []), Merge3Result([], 0))

    def test_one_side_empties_file(self):
        self.assertEqual(merge_blobs(b"a\nb\n", b"", b"a\nb\n"), (b"", True))


# ---------------------------------------------------------------------------
# merge_trees with an in-memory store
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.objs: dict[str, tuple[str, bytes]] = {}

    def write(self, obj_type: str, data: bytes) -> str:
        sha = hashlib.sha1(f"{obj_type} {len(data)}\0".encode() + data).hexdigest()
        self.objs[sha] = (obj_type, data)
        return sha

    def has(self, sha: str) -> bool:
        return sha in self.objs

    def read(self, sha: str) -> tuple[str, bytes]:
        return self.objs[sha]

    def read_typed(self, sha: str, obj_type: str) -> bytes:
        t, d = self.objs[sha]
        assert t == obj_type, (t, obj_type)
        return d


class TreeMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise _IMPORT_ERROR

    def setUp(self):
        self.store = FakeStore()

    def e(self, data: bytes, mode: str = FILE):
        return (mode, self.store.write("blob", data))

    def run_merge(self, b, o, t):
        return M.merge_trees(self.store, b, o, t, "HEAD", "feature")

    def test_trivial_rules(self):
        x, y = self.e(b"x\n"), self.e(b"y\n")
        r = self.run_merge({"same": x, "ours": x, "theirs": x, "deldel": x, "delours": x, "deltheirs": x},
                           {"same": x, "ours": y, "theirs": x, "delours": x, "added": y},
                           {"same": x, "ours": x, "theirs": y, "deltheirs": x, "added": y, "tadd": x})
        self.assertEqual(r.merged, {"same": x, "ours": y, "theirs": y, "added": y, "tadd": x})
        self.assertEqual((r.stages, r.worktree, r.messages), ({}, {}, []))

    def test_modify_modify_clean(self):
        b = self.e(b"1\n2\n3\n4\n5\n")
        o = self.e(b"one\n2\n3\n4\n5\n")
        t = self.e(b"1\n2\n3\n4\nfive\n")
        r = self.run_merge({"f": b}, {"f": o}, {"f": t})
        self.assertEqual(r.stages, {})
        mode, sha = r.merged["f"]
        self.assertEqual(mode, FILE)
        self.assertEqual(self.store.read_typed(sha, "blob"), b"one\n2\n3\n4\nfive\n")

    def test_modify_modify_conflict(self):
        b, o, t = self.e(b"a\n"), self.e(b"ours\n"), self.e(b"theirs\n")
        r = self.run_merge({"f": b}, {"f": o}, {"f": t})
        self.assertNotIn("f", r.merged)
        self.assertEqual(r.stages, {"f": {1: b, 2: o, 3: t}})
        self.assertEqual(r.worktree["f"], (FILE, b"<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\n"))
        self.assertEqual(r.messages, ["CONFLICT (content): Merge conflict in f"])

    def test_add_add(self):
        o, t = self.e(b"ours\n"), self.e(b"theirs\n")
        r = self.run_merge({}, {"n": o, "same": o}, {"n": t, "same": o})
        self.assertEqual(r.merged, {"same": o})
        self.assertEqual(r.stages, {"n": {2: o, 3: t}})
        self.assertEqual(r.messages, ["CONFLICT (add/add): Merge conflict in n"])

    def test_modify_delete_theirs_deleted(self):
        b, o = self.e(b"base\n"), self.e(b"changed\n")
        r = self.run_merge({"f": b}, {"f": o}, {})
        self.assertEqual(r.stages, {"f": {1: b, 2: o}})
        self.assertEqual(r.worktree["f"], (FILE, b"changed\n"))
        self.assertEqual(r.messages, ["CONFLICT (modify/delete): f deleted in feature and modified in HEAD."])

    def test_modify_delete_ours_deleted(self):
        b, t = self.e(b"base\n"), self.e(b"changed\n", EXE)
        r = self.run_merge({"f": b}, {}, {"f": t})
        self.assertEqual(r.stages, {"f": {1: b, 3: t}})
        self.assertEqual(r.worktree["f"], (EXE, b"changed\n"))
        self.assertEqual(r.messages, ["CONFLICT (modify/delete): f deleted in HEAD and modified in feature."])

    def test_mode_only_change(self):
        x = self.e(b"x\n")
        r = self.run_merge({"f": x}, {"f": x}, {"f": (EXE, x[1])})
        self.assertEqual(r.merged, {"f": (EXE, x[1])})

    def test_mode_change_ours_content_change_theirs(self):
        b = self.e(b"a\nb\n")
        t = self.e(b"a\nB\n")
        r = self.run_merge({"f": b}, {"f": (EXE, b[1])}, {"f": t})
        self.assertEqual(r.merged, {"f": (EXE, t[1])})
        self.assertEqual(r.messages, [])

    def test_mode_change_theirs_both_content_changes_merge(self):
        b = self.e(b"1\n2\n3\n4\n")
        o = self.e(b"one\n2\n3\n4\n")
        t = self.e(b"1\n2\n3\nfour\n", EXE)
        r = self.run_merge({"f": b}, {"f": o}, {"f": t})
        mode, sha = r.merged["f"]
        self.assertEqual(mode, EXE)
        self.assertEqual(self.store.read_typed(sha, "blob"), b"one\n2\n3\nfour\n")

    def test_add_add_same_content_different_modes_warns(self):
        o = self.e(b"x\n", EXE)
        r = self.run_merge({}, {"f": o}, {"f": (FILE, o[1])})
        self.assertEqual(r.merged, {"f": o})
        self.assertEqual(len(r.messages), 1)
        self.assertTrue(r.messages[0].startswith("warning:"))

    def test_conflict_worktree_uses_merged_mode(self):
        b = self.e(b"a\n")
        r = self.run_merge({"f": b}, {"f": self.e(b"o\n")}, {"f": self.e(b"t\n", EXE)})
        self.assertEqual(r.worktree["f"][0], EXE)

    def test_binary_conflict_keeps_ours(self):
        b, o, t = self.e(b"\x00base"), self.e(b"\x00ours"), self.e(b"\x00theirs")
        r = self.run_merge({"bin": b}, {"bin": o}, {"bin": t})
        self.assertEqual(r.stages, {"bin": {1: b, 2: o, 3: t}})
        self.assertEqual(r.worktree["bin"], (FILE, b"\x00ours"))
        self.assertEqual(r.messages, ["CONFLICT (binary): Merge conflict in bin"])

    def test_binary_one_side_text_other(self):
        b, o, t = self.e(b"text\n"), self.e(b"more text\n"), self.e(b"\x00\x01")
        r = self.run_merge({"f": b}, {"f": o}, {"f": t})
        self.assertEqual(r.messages, ["CONFLICT (binary): Merge conflict in f"])

    def test_binary_add_add_no_base_stage(self):
        o, t = self.e(b"\x00o"), self.e(b"\x00t")
        r = self.run_merge({}, {"b": o}, {"b": t})
        self.assertEqual(r.stages, {"b": {2: o, 3: t}})

    def test_binary_changed_one_side_with_mode_change_other_is_clean(self):
        b, t = self.e(b"\x00one"), self.e(b"\x00two")
        r = self.run_merge({"f": b}, {"f": (EXE, b[1])}, {"f": t})
        self.assertEqual(r.merged, {"f": (EXE, t[1])})

    def test_file_dir_clash_moves_the_file_aside_as_a_conflict(self):
        a, x, a2 = self.e(b"a\n"), self.e(b"x\n"), self.e(b"a2\n")
        r = self.run_merge({"a": a}, {"a/x": x}, {"a": a2})
        # modify/delete on 'a', and 'a' is a directory in ours: the file moves to a~feature
        self.assertEqual(r.merged, {"a/x": x})
        self.assertEqual(r.stages, {"a~feature": {1: a, 3: a2}})
        self.assertEqual(r.worktree["a~feature"], (FILE, b"a2\n"))
        self.assertIn("CONFLICT (file/directory): directory in the way of a from feature; "
                      "moving it to a~feature instead.", r.messages)

    def test_file_dir_clash_both_added(self):
        f, d = self.e(b"1\n"), self.e(b"2\n")
        r = self.run_merge({}, {"d/f": f}, {"d": d})
        self.assertEqual(r.merged, {"d/f": f})
        self.assertEqual(r.stages, {"d~feature": {3: d}})
        r = self.run_merge({}, {"d": d}, {"d/f": f})
        self.assertEqual(r.stages, {"d~HEAD": {2: d}})

    def test_virtual_merge_keeps_conflict_markers(self):
        b, o, t = self.e(b"base\n"), self.e(b"a\n"), self.e(b"b\n")
        r = M.merge_trees(self.store, {"f": b}, {"f": o}, {"f": t}, "X", "Y", virtual=True)
        self.assertEqual(r.stages, {})
        self.assertEqual(self.store.read_typed(r.merged["f"][1], "blob"),
                         b"<<<<<<< X\na\n=======\nb\n>>>>>>> Y\n")
        # modify/delete falls back to the base version
        r = M.merge_trees(self.store, {"f": b}, {"f": o}, {}, "X", "Y", virtual=True)
        self.assertEqual(r.merged, {"f": b})

    def test_file_replaced_by_dir_on_one_side_is_fine(self):
        a, x = self.e(b"a\n"), self.e(b"x\n")
        r = self.run_merge({"a": a}, {"a": a}, {"a/x": x})
        self.assertEqual(r.merged, {"a/x": x})

    def test_labels_used(self):
        r = M.merge_trees(self.store, {"f": self.e(b"a\n")}, {"f": self.e(b"o\n")}, {"f": self.e(b"t\n")},
                          "OURS", "origin/main")
        self.assertTrue(r.worktree["f"][1].startswith(b"<<<<<<< OURS\n"))
        self.assertTrue(r.worktree["f"][1].endswith(b">>>>>>> origin/main\n"))


# ---------------------------------------------------------------------------
# merge / merge --abort against a real repository
# ---------------------------------------------------------------------------

class RepoCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise _IMPORT_ERROR

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._td.cleanup)
        self.tmp = self._td.name
        env = {
            "MINIGIT_GLOBAL_CONFIG": os.path.join(self.tmp, "globalconfig"),
            "MINIGIT_AUTHOR_NAME": "Tester",
            "MINIGIT_AUTHOR_EMAIL": "tester@example.com",
            "MINIGIT_AUTHOR_DATE": "1700000000 +0000",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.wt = os.path.join(self.tmp, "work")
        os.makedirs(self.wt)
        self.repo, _ = Repository.init(self.wt)
        self._tick = 0

    # -- helpers (do not use M3) --
    def write(self, rel: str, data: bytes) -> None:
        p = os.path.join(self.wt, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def read(self, rel: str) -> bytes:
        with open(os.path.join(self.wt, *rel.split("/")), "rb") as f:
            return f.read()

    def exists(self, rel: str) -> bool:
        return os.path.lexists(os.path.join(self.wt, *rel.split("/")))

    def delete(self, rel: str) -> None:
        os.remove(os.path.join(self.wt, *rel.split("/")))

    def stage(self, *rels: str) -> None:
        with locked_index(self.repo) as idx:
            for rel in rels:
                if os.path.isfile(os.path.join(self.wt, *rel.split("/"))):
                    stage_file(self.repo, idx, rel)
                else:
                    idx.remove(rel)

    def commit(self, message: str) -> str:
        """Mimic 'minigit commit' (section 6): includes MERGE_HEAD and MERGE_MSG."""
        self._tick += 1
        os.environ["MINIGIT_AUTHOR_DATE"] = f"{1700000000 + self._tick * 60} +0000"
        repo = self.repo
        idx = read_index(repo)
        self.assertEqual(idx.unmerged(), {})
        tree = build_tree(repo.objects, idx.flat())
        head = repo.head_commit()
        parents = ([head] if head else []) + ([repo.merge_head()] if repo.merge_head() else [])
        if not message:
            raw = (repo.read_state("MERGE_MSG") or b"").decode()
            message = "\n".join(line for line in raw.split("\n") if not line.startswith("#"))
        sig = repo.identity()
        c = Commit(tree=tree, parents=tuple(parents), author=sig, committer=sig, message=message.rstrip() + "\n")
        sha = repo.objects.write("commit", serialize_commit(c))
        repo.refs.update_head(sha, expected=head)
        repo.refs.write_special("MERGE_HEAD", None)
        repo.remove_state("MERGE_MSG")
        return sha

    def commit_files(self, message: str, files: dict[str, bytes | None]) -> str:
        for rel, data in files.items():
            if data is None:
                if self.exists(rel):
                    self.delete(rel)
            else:
                self.write(rel, data)
        self.stage(*files)
        return self.commit(message)

    def branch(self, name: str, sha: str | None = None) -> None:
        self.repo.refs.update(f"refs/heads/{name}", sha or self.repo.head_commit(), expected=None)

    def switch(self, name: str) -> None:
        repo = self.repo
        new = commit_flat(repo.objects, repo.refs.resolve(f"refs/heads/{name}"))
        with locked_index(repo) as idx:
            checkout_tree(repo, idx, repo.head_flat(), new)
        repo.refs.set_symbolic_head(f"refs/heads/{name}")

    def head(self) -> str:
        return self.repo.head_commit()

    def parents(self, sha: str) -> tuple[str, ...]:
        return parse_commit(self.repo.objects.read_typed(sha, "commit")).parents

    def assert_index_matches_head(self) -> None:
        idx = read_index(self.repo)
        self.assertEqual(idx.unmerged(), {})
        self.assertEqual(idx.flat(), self.repo.head_flat())

    def diverge(self, base_files, main_files, feature_files):
        """base commit on main, then 'feature' and 'main' each get one commit. HEAD ends on main."""
        c0 = self.commit_files("base", base_files)
        self.branch("feature")
        self.switch("feature")
        cf = self.commit_files("feature work", feature_files)
        self.switch("main")
        cm = self.commit_files("main work", main_files)
        return c0, cm, cf


class MergeCommandTests(RepoCase):
    def test_fast_forward(self):
        c0 = self.commit_files("base", {"a.txt": b"a\n"})
        self.branch("feature")
        self.switch("feature")
        c1 = self.commit_files("more", {"b.txt": b"b\n", "a.txt": b"a2\n"})
        self.switch("main")
        self.assertFalse(self.exists("b.txt"))
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.kind, "fast-forward")
        self.assertEqual((out.commit, out.old_head), (c1, c0))
        self.assertEqual(out.messages, [f"Updating {c0[:7]}..{c1[:7]}", "Fast-forward"])
        self.assertEqual(self.head(), c1)
        self.assertEqual(self.repo.refs.current_branch(), "main")
        self.assertEqual(self.read("b.txt"), b"b\n")
        self.assertEqual(self.read("a.txt"), b"a2\n")
        self.assertEqual(self.repo.refs.read_special("ORIG_HEAD"), c0)
        self.assert_index_matches_head()

    def test_already_up_to_date(self):
        c0 = self.commit_files("base", {"a.txt": b"a\n"})
        self.branch("old")
        self.commit_files("next", {"a.txt": b"b\n"})
        head = self.head()
        out = M.merge(self.repo, "old")
        self.assertEqual(out.kind, "up-to-date")
        self.assertEqual(out.messages, ["Already up to date."])
        self.assertEqual(self.head(), head)
        out = M.merge(self.repo, head)
        self.assertEqual(out.kind, "up-to-date")
        self.assertNotEqual(c0, head)

    def test_clean_three_way_merge(self):
        base = b"1\n2\n3\n4\n5\n6\n"
        c0, cm, cf = self.diverge({"f.txt": base, "keep.txt": b"k\n", "gone.txt": b"g\n"},
                                  {"f.txt": b"ONE\n2\n3\n4\n5\n6\n", "main.txt": b"m\n"},
                                  {"f.txt": b"1\n2\n3\n4\n5\nSIX\n", "feat.txt": b"f\n", "gone.txt": None})
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.kind, "merged")
        self.assertEqual(out.messages, ["Merge made by the 'three-way' strategy."])
        self.assertEqual(out.old_head, cm)
        self.assertEqual(self.head(), out.commit)
        self.assertEqual(self.parents(out.commit), (cm, cf))
        commit = self.repo.read_commit(out.commit)
        self.assertEqual(commit.message, "Merge branch 'feature'\n")
        self.assertEqual(self.read("f.txt"), b"ONE\n2\n3\n4\n5\nSIX\n")
        self.assertEqual(self.read("feat.txt"), b"f\n")
        self.assertEqual(self.read("main.txt"), b"m\n")
        self.assertFalse(self.exists("gone.txt"))
        self.assertIsNone(self.repo.merge_head())
        self.assertEqual(self.repo.refs.read_special("ORIG_HEAD"), cm)
        self.assert_index_matches_head()
        self.assertEqual(set(self.repo.head_flat()), {"f.txt", "keep.txt", "main.txt", "feat.txt"})

    def test_custom_message_and_label(self):
        self.diverge({"f": b"x\n"}, {"m": b"1\n"}, {"t": b"2\n"})
        out = M.merge(self.repo, "feature", message="custom merge message\n\n")
        self.assertEqual(self.repo.read_commit(out.commit).message, "custom merge message\n")

    def test_no_ff_creates_merge_commit(self):
        c0 = self.commit_files("base", {"a": b"a\n"})
        self.branch("feature")
        self.switch("feature")
        c1 = self.commit_files("f", {"b": b"b\n"})
        self.switch("main")
        out = M.merge(self.repo, "feature", no_ff=True)
        self.assertEqual(out.kind, "merged")
        self.assertEqual(self.parents(out.commit), (c0, c1))
        self.assertEqual(self.read("b"), b"b\n")
        self.assert_index_matches_head()

    def test_merge_into_unborn_head(self):
        c0 = self.commit_files("base", {"a": b"a\n"})
        self.repo.refs.set_symbolic_head("refs/heads/fresh")
        with locked_index(self.repo) as idx:
            for p in list(idx.paths()):
                idx.remove(p)
        self.delete("a")
        out = M.merge(self.repo, "main")
        self.assertEqual(out.kind, "fast-forward")
        self.assertIsNone(out.old_head)
        self.assertEqual(self.repo.refs.resolve("refs/heads/fresh"), c0)
        self.assertEqual(self.read("a"), b"a\n")
        self.assert_index_matches_head()

    def test_conflict_writes_stages_markers_and_state(self):
        c0, cm, cf = self.diverge({"f.txt": b"a\nb\nc\n", "other": b"o\n"},
                                  {"f.txt": b"a\nmain\nc\n"},
                                  {"f.txt": b"a\nfeature\nc\n", "other": b"o2\n", "new": b"n\n"})
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.kind, "conflict")
        self.assertIsNone(out.commit)
        self.assertEqual(out.conflicts, ["f.txt"])
        self.assertEqual(out.messages, ["CONFLICT (content): Merge conflict in f.txt",
                                        "Automatic merge failed; fix conflicts and then commit the result."])
        self.assertEqual(self.read("f.txt"), b"a\n<<<<<<< HEAD\nmain\n=======\nfeature\n>>>>>>> feature\nc\n")
        self.assertEqual(self.read("other"), b"o2\n")
        self.assertEqual(self.read("new"), b"n\n")
        idx = read_index(self.repo)
        stages = idx.unmerged()["f.txt"]
        self.assertEqual(sorted(stages), [1, 2, 3])
        b, o, t = (commit_flat(self.repo.objects, c)["f.txt"] for c in (c0, cm, cf))
        self.assertEqual((stages[1].sha, stages[2].sha, stages[3].sha), (b[1], o[1], t[1]))
        self.assertIsNone(idx.get("f.txt", 0))
        self.assertEqual(idx.get("other").sha, commit_flat(self.repo.objects, cf)["other"][1])
        self.assertEqual(self.repo.merge_head(), cf)
        self.assertEqual(self.repo.refs.read_special("ORIG_HEAD"), cm)
        msg = self.repo.read_state("MERGE_MSG").decode()
        self.assertTrue(msg.startswith("Merge branch 'feature'\n"))
        self.assertIn("# Conflicts:\n#\tf.txt\n", msg)
        self.assertEqual(self.head(), cm)

    def test_merge_refused_while_merge_in_progress(self):
        self.diverge({"f": b"a\n"}, {"f": b"m\n"}, {"f": b"t\n"})
        self.assertEqual(M.merge(self.repo, "feature").kind, "conflict")
        with self.assertRaises(MinigitError) as cm:
            M.merge(self.repo, "feature")
        self.assertIn("you have not concluded your merge (MERGE_HEAD exists)", cm.exception.message)

    def test_resolve_via_index_then_commit(self):
        c0, cm, cf = self.diverge({"f": b"a\n"}, {"f": b"m\n"}, {"f": b"t\n"})
        M.merge(self.repo, "feature")
        self.write("f", b"resolved\n")
        self.stage("f")
        self.assertEqual(read_index(self.repo).unmerged(), {})
        sha = self.commit("")
        self.assertEqual(self.parents(sha), (cm, cf))
        self.assertEqual(self.repo.read_commit(sha).message, "Merge branch 'feature'\n")
        self.assertIsNone(self.repo.merge_head())
        self.assertEqual(self.read("f"), b"resolved\n")
        # A later merge of the same branch is now a no-op.
        self.assertEqual(M.merge(self.repo, "feature").kind, "up-to-date")

    def test_abort_restores_byte_for_byte_and_keeps_unrelated_dirty(self):
        orig = b"a\r\nb\r\nc\r\n"
        c0, cm, cf = self.diverge({"f": orig, "other": b"o\n", "dirty": b"d\n"},
                                  {"f": b"a\r\nmain\r\nc\r\n"},
                                  {"f": b"a\r\nfeature\r\nc\r\n", "other": b"o2\n", "sub/new": b"n\n"})
        self.write("dirty", b"local edit\n")
        self.write("untracked.txt", b"u\n")
        before_f = self.read("f")
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.kind, "conflict")
        self.assertIn(b"<<<<<<< HEAD\r\n", self.read("f"))
        M.merge_abort(self.repo)
        self.assertEqual(self.read("f"), before_f)
        self.assertEqual(self.read("other"), b"o\n")
        self.assertFalse(self.exists("sub/new"))
        self.assertFalse(self.exists("sub"))
        self.assertEqual(self.read("dirty"), b"local edit\n")
        self.assertEqual(self.read("untracked.txt"), b"u\n")
        self.assertIsNone(self.repo.merge_head())
        self.assertIsNone(self.repo.read_state("MERGE_MSG"))
        self.assertEqual(self.head(), cm)
        self.assert_index_matches_head()

    def test_abort_after_partial_resolution(self):
        self.diverge({"f": b"a\n", "g": b"a\n"}, {"f": b"m\n", "g": b"m\n"}, {"f": b"t\n", "g": b"t\n"})
        M.merge(self.repo, "feature")
        self.write("f", b"fixed\n")
        self.stage("f")
        M.merge_abort(self.repo)
        self.assertEqual((self.read("f"), self.read("g")), (b"m\n", b"m\n"))
        self.assert_index_matches_head()

    def test_abort_without_merge(self):
        self.commit_files("base", {"a": b"a\n"})
        with self.assertRaises(MinigitError) as cm:
            M.merge_abort(self.repo)
        self.assertEqual(cm.exception.message, "there is no merge to abort (MERGE_HEAD missing)")

    def test_safety_refusal_dirty_file(self):
        c0, cm, cf = self.diverge({"f": b"1\n2\n3\n", "g": b"g\n"}, {"g": b"g2\n"}, {"f": b"1\n2\nthree\n"})
        self.write("f", b"my local edit\n")
        with self.assertRaises(CheckoutConflictError) as ctx:
            M.merge(self.repo, "feature")
        self.assertEqual(ctx.exception.paths, ["f"])
        self.assertIn("would be overwritten by merge", ctx.exception.message)
        self.assertEqual(self.read("f"), b"my local edit\n")
        self.assertEqual(self.head(), cm)
        self.assertIsNone(self.repo.merge_head())
        self.assert_index_matches_head()

    def test_safety_refusal_untracked_file_in_the_way(self):
        self.diverge({"f": b"x\n"}, {"m": b"m\n"}, {"new.txt": b"theirs\n"})
        self.write("new.txt", b"untracked\n")
        with self.assertRaises(CheckoutConflictError) as ctx:
            M.merge(self.repo, "feature")
        self.assertEqual(ctx.exception.paths, ["new.txt"])
        self.assertEqual(ctx.exception.message,
                         "The following untracked working tree files would be overwritten by merge:\n"
                         "\tnew.txt\nPlease move or remove them before you merge.")
        self.assertEqual(self.read("new.txt"), b"untracked\n")
        self.assertIsNone(self.repo.merge_head())
        self.assert_index_matches_head()

    def test_untracked_file_in_the_way_of_a_fast_forward_uses_untracked_wording(self):
        self.commit_files("base", {"a.txt": b"a\n"})
        self.branch("feature")
        self.switch("feature")
        self.commit_files("more", {"b.txt": b"b\n"})
        self.switch("main")
        self.write("b.txt", b"mine\n")
        with self.assertRaises(CheckoutConflictError) as ctx:
            M.merge(self.repo, "feature")
        self.assertEqual(ctx.exception.paths, ["b.txt"])
        self.assertIn("The following untracked working tree files would be overwritten by merge:\n\tb.txt\n"
                      "Please move or remove them before you merge.", ctx.exception.message)
        self.assertNotIn("local changes", ctx.exception.message)
        self.assertEqual(self.read("b.txt"), b"mine\n")

    def test_detached_head_merge_by_sha(self):
        c0, cm, cf = self.diverge({"f": b"1\n2\n3\n"}, {"f": b"one\n2\n3\n"}, {"f": b"1\n2\nthree\n"})
        self.repo.refs.set_detached_head(cm)
        out = M.merge(self.repo, cf)
        self.assertEqual(out.kind, "merged")
        self.assertIsNone(self.repo.refs.head_target())
        self.assertEqual(self.head(), out.commit)
        self.assertEqual(self.repo.refs.resolve("refs/heads/main"), cm)
        self.assertEqual(self.repo.read_commit(out.commit).message, f"Merge commit '{cf}'\n")

    def test_safety_refusal_untracked_dir_in_the_way(self):
        self.diverge({"f": b"x\n"}, {"m": b"m\n"}, {"thing": b"theirs\n"})
        self.write("thing/inner.txt", b"u\n")
        with self.assertRaises(CheckoutConflictError) as ctx:
            M.merge(self.repo, "feature")
        self.assertEqual(ctx.exception.paths, ["thing"])
        self.assertEqual(ctx.exception.message,
                         "Updating the following directories would lose untracked files in them:\n\tthing\n")
        self.assertEqual(self.read("thing/inner.txt"), b"u\n")

    def test_dirty_conflicting_file_refused(self):
        self.diverge({"f": b"a\n"}, {"f": b"m\n"}, {"f": b"t\n"})
        self.write("f", b"wip\n")
        with self.assertRaises(CheckoutConflictError):
            M.merge(self.repo, "feature")
        self.assertEqual(self.read("f"), b"wip\n")

    def test_dirty_unrelated_file_survives_clean_merge(self):
        self.diverge({"f": b"1\n2\n3\n", "u": b"u\n"}, {"f": b"one\n2\n3\n"}, {"f": b"1\n2\nthree\n"})
        self.write("u", b"dirty\n")
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.kind, "merged")
        self.assertEqual(self.read("u"), b"dirty\n")
        self.assertEqual(self.read("f"), b"one\n2\nthree\n")

    def test_staged_changes_refused(self):
        self.diverge({"f": b"x\n"}, {"m": b"m\n"}, {"t": b"t\n"})
        self.write("f", b"staged\n")
        self.stage("f")
        with self.assertRaises(MinigitError) as cm:
            M.merge(self.repo, "feature")
        self.assertEqual(cm.exception.message, "you have staged changes; commit them first: f")

    def test_fast_forward_keeps_unrelated_staged_change(self):
        self.commit_files("base", {"a.txt": b"a\n", "u.txt": b"u\n"})
        self.branch("feature")
        self.switch("feature")
        c1 = self.commit_files("more", {"a.txt": b"a2\n", "f.txt": b"f\n"})
        self.switch("main")
        self.write("u.txt", b"staged\n")
        self.stage("u.txt")
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.kind, "fast-forward")
        self.assertEqual(self.head(), c1)
        self.assertEqual(self.read("a.txt"), b"a2\n")
        self.assertEqual(self.read("u.txt"), b"staged\n")
        with locked_index(self.repo) as idx:
            flat = idx.flat()
        self.assertEqual(flat["u.txt"][1], M.hash_object("blob", b"staged\n"))
        self.assertEqual(flat["f.txt"], commit_flat(self.repo.objects, c1)["f.txt"])

    def test_fast_forward_refuses_staged_change_on_a_path_it_changes(self):
        self.commit_files("base", {"a.txt": b"a\n", "u.txt": b"u\n"})
        self.branch("feature")
        self.switch("feature")
        self.commit_files("more", {"a.txt": b"a2\n", "f.txt": b"f\n"})
        self.switch("main")
        before = self.head()
        self.write("a.txt", b"mine\n")
        self.stage("a.txt")
        self.write("f.txt", b"also mine\n")
        self.stage("f.txt")
        with self.assertRaises(CheckoutConflictError) as cm:
            M.merge(self.repo, "feature")
        self.assertEqual(cm.exception.paths, ["a.txt", "f.txt"])
        self.assertEqual(cm.exception.message,
                         "Your local changes to the following files would be overwritten by merge:\n"
                         "\ta.txt\n\tf.txt\nPlease commit your changes or restore them before you merge.")
        self.assertEqual(self.head(), before)
        self.assertEqual(self.read("a.txt"), b"mine\n")

    def test_unrelated_histories_refused(self):
        self.commit_files("base", {"a": b"a\n"})
        self.repo.refs.set_symbolic_head("refs/heads/other")
        with locked_index(self.repo) as idx:
            for p in list(idx.paths()):
                idx.remove(p)
        self.delete("a")
        self.commit_files("other root", {"b": b"b\n"})
        with self.assertRaises(MinigitError) as cm:
            M.merge(self.repo, "main")
        self.assertEqual(cm.exception.message, "refusing to merge unrelated histories")
        self.assertEqual(cm.exception.hint, "use --allow-unrelated-histories if you really want to join them")

    def test_allow_unrelated_histories(self):
        # QA bug 12: two people each made a root commit in a fresh clone of an empty repo
        main_root = self.commit_files("alice root", {"a": b"a\n", "same": b"s\n", "both": b"alice\n"})
        self.repo.refs.set_symbolic_head("refs/heads/other")
        with locked_index(self.repo) as idx:
            for p in list(idx.paths()):
                idx.remove(p)
        for p in ("a", "same", "both"):
            self.delete(p)
        other_root = self.commit_files("bob root", {"b": b"b\n", "same": b"s\n", "both": b"bob\n"})
        out = M.merge(self.repo, "main", allow_unrelated=True)
        self.assertEqual(out.kind, "conflict")  # add/add with different content, as in git
        self.assertEqual(out.conflicts, ["both"])
        self.assertEqual(self.read("a"), b"a\n")
        self.assertEqual(self.read("b"), b"b\n")
        self.assertEqual(self.read("same"), b"s\n")
        self.assertIn(b"<<<<<<< HEAD\nbob\n=======\nalice\n>>>>>>> main\n", self.read("both"))
        self.assertEqual(self.repo.merge_head(), main_root)
        self.assertEqual(self.repo.head_commit(), other_root)
        M.merge_abort(self.repo)
        # without a conflicting path the merge commit is made directly
        self.commit_files("drop both", {"both": None})
        out = M.merge(self.repo, "main", allow_unrelated=True)
        self.assertEqual(out.kind, "merged")
        parents = self.repo.objects.read_typed(out.commit, "commit")
        self.assertIn(main_root.encode(), parents)

    def test_unknown_revision(self):
        self.commit_files("base", {"a": b"a\n"})
        with self.assertRaises(MinigitError):
            M.merge(self.repo, "nope")

    def test_modify_delete_through_merge(self):
        c0, cm, cf = self.diverge({"f": b"base\n", "g": b"g\n"}, {"f": b"modified\n"}, {"f": None})
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.kind, "conflict")
        self.assertEqual(out.messages[0], "CONFLICT (modify/delete): f deleted in feature and modified in HEAD.")
        self.assertEqual(self.read("f"), b"modified\n")
        self.assertEqual(sorted(read_index(self.repo).unmerged()["f"]), [1, 2])
        # resolve by keeping the file
        self.stage("f")
        sha = self.commit("")
        self.assertEqual(self.parents(sha), (cm, cf))

    def test_delete_modify_through_merge_writes_theirs(self):
        self.diverge({"f": b"base\n", "g": b"g\n"}, {"f": None}, {"f": b"theirs\n"})
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.messages[0], "CONFLICT (modify/delete): f deleted in HEAD and modified in feature.")
        self.assertEqual(self.read("f"), b"theirs\n")
        self.assertEqual(sorted(read_index(self.repo).unmerged()["f"]), [1, 3])
        M.merge_abort(self.repo)
        self.assertFalse(self.exists("f"))
        self.assert_index_matches_head()

    def test_binary_conflict_keeps_ours_in_worktree(self):
        self.diverge({"img": b"\x00base"}, {"img": b"\x00main"}, {"img": b"\x00feat"})
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.messages[0], "CONFLICT (binary): Merge conflict in img")
        self.assertEqual(self.read("img"), b"\x00main")

    def test_file_dir_clash_is_a_resolvable_conflict(self):
        c0, cm, cf = self.diverge({"base": b"b\n"}, {"foo": b"foofile\n"}, {"foo/bar": b"bar\n"})
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.kind, "conflict")
        self.assertEqual(out.conflicts, ["foo~HEAD"])
        self.assertIn("CONFLICT (file/directory): directory in the way of foo from HEAD; "
                      "moving it to foo~HEAD instead.", out.messages)
        self.assertEqual(self.read("foo/bar"), b"bar\n")
        self.assertEqual(self.read("foo~HEAD"), b"foofile\n")
        self.assertEqual(self.repo.merge_head(), cf)
        idx = read_index(self.repo)
        self.assertEqual(sorted(idx.unmerged()["foo~HEAD"]), [2])
        self.assertIsNone(idx.get("foo"))
        # resolve: keep the file under a new name, then commit
        self.stage("foo~HEAD")
        sha = self.commit("")
        self.assertEqual(self.parents(sha), (cm, cf))
        self.assertEqual(sorted(self.repo.head_flat()), ["base", "foo/bar", "foo~HEAD"])

    def test_file_dir_clash_abort_restores_ours(self):
        self.diverge({"a": b"a\n", "z": b"z\n"}, {"a": None, "a/x": b"x\n"}, {"a": b"a2\n", "z": b"z2\n"})
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.kind, "conflict")
        self.assertEqual(self.read("a~feature"), b"a2\n")
        self.assertEqual(self.read("z"), b"z2\n")
        M.merge_abort(self.repo)
        self.assertEqual(self.read("a/x"), b"x\n")
        self.assertEqual(self.read("z"), b"z\n")
        self.assertFalse(self.exists("a~feature"))
        self.assert_index_matches_head()

    def test_criss_cross_merge_conflicts_when_resolutions_differ(self):
        c0 = self.commit_files("C0", {"f.txt": b"base\n", "g.txt": b"1\n2\n3\n"})
        self.branch("A")
        self.branch("B")
        self.switch("A")
        a1 = self.commit_files("A1", {"f.txt": b"a\n"})
        self.switch("B")
        b1 = self.commit_files("B1", {"f.txt": b"b\n"})
        self.switch("A")
        self.assertEqual(M.merge(self.repo, "B").kind, "conflict")
        self.commit_files("A2 keep a", {"f.txt": b"a\n"})
        self.switch("B")
        self.assertEqual(M.merge(self.repo, a1).kind, "conflict")
        self.commit_files("B2 keep b", {"f.txt": b"b\n"})
        # an unrelated clean change on A still merges cleanly
        self.switch("A")
        self.commit_files("A3", {"g.txt": b"1\n2\nthree\n"})
        self.switch("B")
        from minigit.graph import merge_bases
        self.assertEqual(sorted(merge_bases(self.repo.objects, self.head(),
                                            self.repo.refs.resolve("refs/heads/A"))), sorted([a1, b1]))
        out = M.merge(self.repo, "A")
        self.assertEqual(out.kind, "conflict")
        self.assertEqual(out.conflicts, ["f.txt"])
        self.assertEqual(self.read("f.txt"), b"<<<<<<< HEAD\nb\n=======\na\n>>>>>>> A\n")
        self.assertEqual(self.read("g.txt"), b"1\n2\nthree\n")

    def test_criss_cross_merge_same_resolution_is_clean(self):
        self.commit_files("C0", {"f.txt": b"base\n", "g.txt": b"g\n"})
        self.branch("A")
        self.branch("B")
        self.switch("A")
        a1 = self.commit_files("A1", {"f.txt": b"a\n"})
        self.switch("B")
        self.commit_files("B1", {"f.txt": b"b\n"})
        self.switch("A")
        M.merge(self.repo, "B")
        self.commit_files("A2", {"f.txt": b"ab\n"})
        self.switch("B")
        M.merge(self.repo, a1)
        self.commit_files("B2", {"f.txt": b"ab\n", "g.txt": b"g2\n"})
        out = M.merge(self.repo, "A")
        self.assertEqual(out.kind, "merged")
        self.assertEqual(self.read("f.txt"), b"ab\n")
        self.assertEqual(self.read("g.txt"), b"g2\n")

    def test_file_replaced_by_directory_from_theirs(self):
        self.diverge({"a": b"a\n", "m": b"m\n"}, {"m": b"m2\n"}, {"a": None, "a/x": b"x\n"})
        out = M.merge(self.repo, "feature")
        self.assertEqual(out.kind, "merged")
        self.assertEqual(self.read("a/x"), b"x\n")
        self.assert_index_matches_head()

    def test_label_in_markers_and_messages(self):
        self.diverge({"f": b"a\n"}, {"f": b"m\n"}, {"f": b"t\n"})
        out = M.merge(self.repo, "refs/heads/feature", label="origin/main",
                      message="Merge branch 'main' of http://x/proj")
        self.assertEqual(out.kind, "conflict")
        self.assertTrue(self.read("f").endswith(b">>>>>>> origin/main\n"))
        self.assertTrue(self.repo.read_state("MERGE_MSG").decode().startswith(
            "Merge branch 'main' of http://x/proj\n"))

    def test_missing_identity_fails_before_touching_worktree(self):
        self.diverge({"f": b"1\n2\n3\n"}, {"f": b"one\n2\n3\n"}, {"f": b"1\n2\nthree\n"})
        for k in ("MINIGIT_AUTHOR_NAME", "MINIGIT_AUTHOR_EMAIL"):
            os.environ.pop(k, None)
        with self.assertRaises(MinigitError):
            M.merge(self.repo, "feature")
        self.assertEqual(self.read("f"), b"one\n2\n3\n")
        self.assert_index_matches_head()

    def test_default_merge_message(self):
        c0 = self.commit_files("base", {"a": b"a\n"})
        self.branch("feature")
        self.repo.refs.update("refs/remotes/origin/main", c0, expected=None)
        self.assertEqual(M.default_merge_message(self.repo, "feature"), "Merge branch 'feature'")
        self.assertEqual(M.default_merge_message(self.repo, "origin/main"),
                         "Merge remote-tracking branch 'origin/main'")
        self.assertEqual(M.default_merge_message(self.repo, c0[:8]), f"Merge commit '{c0[:8]}'")

    @unittest.skipIf(os.name == "nt", "exec bit is POSIX only")
    def test_exec_bit_applied_by_clean_merge(self):
        self.diverge({"s.sh": b"echo 1\n", "x": b"x\n"}, {"x": b"x2\n"}, {})
        # theirs: chmod +x on s.sh
        self.switch("feature")
        os.chmod(os.path.join(self.wt, "s.sh"), 0o755)
        self.stage("s.sh")
        self.commit("chmod")
        self.switch("main")
        M.merge(self.repo, "feature")
        self.assertTrue(os.stat(os.path.join(self.wt, "s.sh")).st_mode & 0o100)
        self.assertEqual(self.repo.head_flat()["s.sh"][0], EXE)


if __name__ == "__main__":
    unittest.main()

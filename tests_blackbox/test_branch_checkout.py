"""Black-box QA suite for minigit's branch / switch / checkout area.

Every test drives the real CLI (``python -m minigit ...``) as a subprocess in a
fresh temporary directory. Expectations come from SPEC.md (section 4 ref rules,
section 6 "branch" and "switch / checkout", the checkout_tree plan) and, where
the spec is silent, from how real git behaves.

Fixture repositories that many tests share are built once per run (through the
CLI) and copied into each test's own temp dir, which keeps the suite fast on
Windows where every CLI call costs a fresh interpreter.

Run from the project root:

    python -m unittest tests_blackbox.test_branch_checkout
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 60

OVERWRITE_HEAD = "Your local changes to the following files would be overwritten by checkout:"
OVERWRITE_TAIL = "Please commit your changes or restore them before you switch branches."
MERGE_BLOCK = "merge in progress; commit or run 'minigit merge --abort'"

_TEMPLATE_ROOT: str | None = None
_TEMPLATES: dict[str, object] = {}


def setUpModule():
    global _TEMPLATE_ROOT
    _TEMPLATE_ROOT = tempfile.mkdtemp(prefix="mgbb-bc-tpl-")


def tearDownModule():
    if _TEMPLATE_ROOT:
        shutil.rmtree(_TEMPLATE_ROOT, ignore_errors=True)


class Result:
    """One CLI invocation; ``out``/``err`` are decoded with newlines normalised."""

    def __init__(self, args, proc):
        self.args = list(args)
        self.code = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace").replace("\r\n", "\n")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace").replace("\r\n", "\n")

    @property
    def text(self) -> str:
        return self.out + self.err

    def __str__(self) -> str:
        return (f"$ minigit {' '.join(self.args)}  -> exit {self.code}\n"
                f"--- stdout ---\n{self.out}--- stderr ---\n{self.err}")


class MinigitCase(unittest.TestCase):
    """Fresh repo at ``self.repo`` (HEAD -> unborn main) with an isolated env."""

    def setUp(self):
        td = tempfile.TemporaryDirectory(prefix="mgbb-bc-", ignore_cleanup_errors=True)
        self.addCleanup(td.cleanup)
        self.tmp = os.path.realpath(td.name)
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        env = dict(os.environ)
        env.update({
            "HOME": home,
            "USERPROFILE": home,
            "MINIGIT_GLOBAL_CONFIG": os.path.join(home, ".minigitconfig"),
            "MINIGIT_CREDENTIALS": os.path.join(home, ".minigit-credentials.json"),
            "MINIGIT_AUTHOR_NAME": "QA Tester",
            "MINIGIT_AUTHOR_EMAIL": "qa@example.com",
            "MINIGIT_AUTHOR_DATE": "1700000000 +0000",
            "PYTHONIOENCODING": "utf-8",
        })
        for key in ("MINIGIT_DEBUG", "MINIGIT_USER", "MINIGIT_TOKEN"):
            env.pop(key, None)
        prev = env.get("PYTHONPATH")
        env["PYTHONPATH"] = ROOT if not prev else ROOT + os.pathsep + prev
        self.env = env
        self.repo = os.path.join(self.tmp, "repo")
        self.mg("init", self.repo, cwd=self.tmp)

    # -- running the CLI ---------------------------------------------------
    def run_mg(self, *args, cwd=None) -> Result:
        args = [str(a) for a in args]
        proc = subprocess.run(
            [sys.executable, "-m", "minigit", *args],
            cwd=cwd or self.repo, env=self.env, capture_output=True,
            stdin=subprocess.DEVNULL, timeout=TIMEOUT,
        )
        return Result(args, proc)

    def mg(self, *args, cwd=None) -> Result:
        """Run and require exit 0."""
        res = self.run_mg(*args, cwd=cwd)
        if res.code != 0:
            self.fail(f"expected exit 0\n{res}")
        return res

    def mg_fail(self, *args, code=1, cwd=None) -> Result:
        """Run and require the given non-zero exit code and no crash."""
        res = self.run_mg(*args, cwd=cwd)
        self.assertNoCrash(res)
        self.assertEqual(res.code, code, f"expected exit {code}\n{res}")
        return res

    def assertNoCrash(self, res: Result):
        self.assertNotIn("internal error", res.text, f"internal error\n{res}")
        self.assertNotIn("Traceback", res.text, f"traceback\n{res}")

    # -- files ---------------------------------------------------------------
    def p(self, rel: str) -> str:
        return os.path.join(self.repo, *rel.split("/"))

    def w(self, rel: str, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        path = self.p(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    def r(self, rel: str) -> bytes:
        with open(self.p(rel), "rb") as fh:
            return fh.read()

    def exists(self, rel: str) -> bool:
        return os.path.lexists(self.p(rel))

    def delete(self, rel: str):
        """Remove a file and prune now-empty parent directories (like git)."""
        path = self.p(rel)
        os.remove(path)
        parent = os.path.dirname(path)
        while os.path.normcase(parent) != os.path.normcase(self.repo) and not os.listdir(parent):
            os.rmdir(parent)
            parent = os.path.dirname(parent)

    # -- repo state ----------------------------------------------------------
    def commit(self, msg: str) -> str:
        self.mg("add", ".")
        self.mg("commit", "-m", msg)
        return self.rev("HEAD")

    def rev(self, spec: str) -> str:
        out = self.mg("rev-parse", spec).out.strip()
        self.assertRegex(out, r"^[0-9a-f]{40}$")
        return out

    def head(self) -> str:
        with open(os.path.join(self.repo, ".minigit", "HEAD"), "rb") as fh:
            return fh.read().decode("utf-8").strip()

    def porcelain(self) -> str:
        return self.mg("status", "--porcelain").out

    def ref_files(self) -> set[str]:
        """Every file under .minigit/refs/heads (catches stray/lock files)."""
        base = os.path.join(self.repo, ".minigit", "refs", "heads")
        found = set()
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                rel = os.path.relpath(os.path.join(dirpath, name), base)
                found.add(rel.replace(os.sep, "/"))
        return found

    def write_ref(self, name: str, sha: str):
        path = os.path.join(self.repo, ".minigit", *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write((sha + "\n").encode())

    def assertOnBranch(self, name: str):
        self.assertEqual(self.head(), f"ref: refs/heads/{name}")

    # -- shared fixtures -----------------------------------------------------
    def from_template(self, name: str, builder):
        """Build ``builder`` once per run into a template repo, then copy it here."""
        tpl = os.path.join(_TEMPLATE_ROOT, name)
        if name not in _TEMPLATES:
            if os.path.exists(tpl):
                shutil.rmtree(tpl)
            mine = self.repo
            self.repo = tpl
            try:
                self.mg("init", tpl, cwd=self.tmp)
                value = builder()
            finally:
                self.repo = mine
            _TEMPLATES[name] = value
        shutil.rmtree(self.repo)
        shutil.copytree(tpl, self.repo)   # copy2 keeps mtimes, so the stat cache still holds
        return _TEMPLATES[name]

    def two_branches(self) -> tuple[str, str]:
        """main = C1, feature = F1 (child of C1). Leaves HEAD on a clean main.

        C1: common.txt staged.txt keep.txt diff.txt gone.txt sub/only_main.txt
        F1: diff.txt changed, new.txt added, gone.txt + sub/only_main.txt deleted
        """
        def build():
            self.w("common.txt", "common\n")
            self.w("staged.txt", "stage me\n")
            self.w("keep.txt", "keep\n")
            self.w("diff.txt", "base\n")
            self.w("gone.txt", "gone on feature\n")
            self.w("sub/only_main.txt", "main only\n")
            c1 = self.commit("c1")
            self.mg("switch", "-c", "feature")
            self.w("diff.txt", "feature\n")
            self.w("new.txt", "feature new\n")
            self.delete("gone.txt")
            self.delete("sub/only_main.txt")
            f1 = self.commit("f1")
            self.mg("switch", "main")
            self.assertOnBranch("main")
            self.assertEqual(self.r("diff.txt"), b"base\n", "fixture: switch main failed")
            self.assertEqual(self.porcelain(), "", "fixture: main not clean")
            return c1, f1
        return self.from_template("two_branches", build)

    def linear(self) -> tuple[str, str]:
        """main: C1 'first' (a.txt=v1) -> C2 'second' (a.txt=v2, b.txt=b)."""
        def build():
            self.w("a.txt", "v1\n")
            c1 = self.commit("first")
            self.w("a.txt", "v2\n")
            self.w("b.txt", "b\n")
            c2 = self.commit("second")
            return c1, c2
        return self.from_template("linear", build)


# ===========================================================================
# branch: create / list
# ===========================================================================
class BranchCreateListTests(MinigitCase):

    def test_list_create_sorted_and_verbose(self):
        c1, c2 = self.linear()
        self.assertEqual(self.mg("branch").out, "* main\n")
        for name in ("zeta", "alpha", "beta/x"):
            self.mg("branch", name)
        self.assertEqual(self.mg("branch").out, "  alpha\n  beta/x\n* main\n  zeta\n")
        self.assertEqual(self.rev("beta/x"), c2)
        self.assertOnBranch("main")  # creating never switches
        # START points: HEAD~N, a short sha, another branch.
        self.mg("branch", "old", "HEAD~1")
        self.mg("branch", "old2", c1[:7])
        self.mg("branch", "same", "zeta")
        self.assertEqual(self.rev("old"), c1)
        self.assertEqual(self.rev("old2"), c1)
        self.assertEqual(self.rev("same"), c2)
        self.assertOnBranch("main")
        self.assertEqual(self.r("a.txt"), b"v2\n", "branch START must not touch the worktree")
        self.assertEqual(self.porcelain(), "")
        verbose = self.mg("branch", "-v").out
        self.assertRegex(verbose, rf"(?m)^\* main\s+{c2[:7]} second$")
        self.assertRegex(verbose, rf"(?m)^  old\s+{c1[:7]} first$")

    def test_create_existing_refused_and_ref_unchanged(self):
        c1, c2 = self.linear()
        self.mg("branch", "topic", c1)
        for argv in (["branch", "topic"], ["branch", "topic", "HEAD"], ["switch", "-c", "topic"],
                     ["checkout", "-b", "topic"]):
            with self.subTest(argv=argv):
                res = self.mg_fail(*argv)
                self.assertIn("a branch named 'topic' already exists", res.err)
                self.assertEqual(self.rev("topic"), c1, "existing branch was moved")
                self.assertOnBranch("main")
        res = self.mg_fail("branch", "main")
        self.assertIn("a branch named 'main' already exists", res.err)
        self.assertEqual(self.rev("main"), c2)

    def test_invalid_names_refused_without_side_effects(self):
        self.linear()
        bad = ["has space", "a..b", "x.lock", ".hidden", "dir/.x", "trail/", "a//b",
               "a~1", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "a@{b", "tab\there", "HEAD"]
        for name in bad:
            with self.subTest(name=name):
                res = self.run_mg("branch", name)
                self.assertNoCrash(res)
                self.assertEqual(res.code, 1, f"bad branch name {name!r} accepted\n{res}")
                self.assertEqual(self.ref_files(), {"main"}, f"{name!r} left ref/lock files behind")
        for name in ("a b", "x.lock", "a..b"):
            with self.subTest(switch_c=name):
                self.mg_fail("switch", "-c", name)
                self.assertOnBranch("main")
        self.assertEqual(self.ref_files(), {"main"})

    def test_unusual_but_valid_names_round_trip(self):
        c1, c2 = self.linear()
        names = ["fix/issue-42", "release_1.0", "a.b-c_d", "ünïcödé"]
        for name in names:
            with self.subTest(name=name):
                self.mg("branch", name, c1)
                self.assertIn(f"  {name}\n", self.mg("branch").out)
                res = self.mg("switch", name)
                self.assertIn(f"Switched to branch '{name}'", res.out)
                self.assertOnBranch(name)
                self.assertEqual(self.r("a.txt"), b"v1\n")
                self.mg("switch", "main")
        # Whatever happened above, HEAD, index and worktree must still agree.
        if self.head() == "ref: refs/heads/main":
            self.assertEqual(self.r("a.txt"), b"v2\n")
            self.assertEqual(self.porcelain(), "", "a failed switch left main's index/worktree changed")

    def test_unborn_head_branch_operations(self):
        res = self.mg("branch")  # git prints nothing; must not crash
        self.assertNotIn("feature", res.out)
        self.mg_fail("branch", "feature")
        self.assertEqual(self.ref_files(), set(), "branch on unborn HEAD created a ref")
        self.assertOnBranch("main")
        # switch -c on an unborn HEAD only retargets HEAD and keeps the index.
        self.w("a.txt", "hello\n")
        self.mg("add", "a.txt")
        res = self.mg("switch", "-c", "dev")
        self.assertIn("Switched to a new branch 'dev'", res.out)
        self.assertOnBranch("dev")
        self.assertEqual(self.porcelain(), "A  a.txt\n")
        out = self.mg("commit", "-m", "root").out
        self.assertTrue(out.startswith("[dev (root-commit) "), out)
        self.assertEqual(self.ref_files(), {"dev"}, "commit landed on the wrong branch")

    def test_bad_start_point_refused(self):
        self.linear()
        blob = self.mg("rev-parse", "HEAD:a.txt").out.strip()
        res = self.mg_fail("branch", "x", "no-such-rev")
        self.assertIn("unknown revision", res.err)
        self.mg_fail("branch", "y", "HEAD:a.txt")
        res = self.mg_fail("branch", "z", blob)
        self.assertIn("is not a commit", res.err)
        self.mg_fail("branch", "q", "HEAD~5")
        self.mg_fail("switch", "-c", "w", "no-such-rev")
        self.mg_fail("checkout", "-b", "v", blob)
        self.assertEqual(self.ref_files(), {"main"})
        self.assertOnBranch("main")

    def test_nested_names_and_file_dir_clash(self):
        self.linear()
        self.mg("branch", "feat/one")
        self.mg_fail("branch", "feat")             # refs/heads/feat is a directory
        self.mg_fail("branch", "feat/one/two")     # refs/heads/feat/one is a file
        self.mg_fail("switch", "-c", "feat")
        self.assertEqual(self.mg("branch").out, "  feat/one\n* main\n")
        self.mg("branch", "-d", "feat/one")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".minigit", "refs", "heads", "feat")),
                         "empty refs/heads/feat directory was not pruned")
        self.mg("branch", "feat")
        self.assertEqual(self.mg("branch").out, "  feat\n* main\n")

    @unittest.skipUnless(os.name == "nt", "Windows filesystem quirks")
    def test_windows_ref_name_quirks(self):
        c1, c2 = self.linear()
        with self.subTest("case-only different name aliases on NTFS"):
            self.mg("branch", "feature", c1)
            self.mg_fail("branch", "Feature")
            self.assertEqual(self.rev("feature"), c1, "creating 'Feature' moved 'feature'")
        with self.subTest("reserved device name 'nul'"):
            res = self.run_mg("branch", "nul")
            self.assertNoCrash(res)
            if res.code == 0:
                # Accepted: it must then be a real, usable branch.
                self.assertIn("  nul\n", self.mg("branch").out, "branch 'nul' reported success but is missing")
                self.assertEqual(self.rev("refs/heads/nul"), c2)
            else:
                self.assertEqual(res.code, 1, str(res))
            self.assertEqual(self.rev("main"), c2)
            self.assertNotIn("nul.lock", self.ref_files(), "stale nul.lock left behind")


# ===========================================================================
# branch: delete
# ===========================================================================
class BranchDeleteTests(MinigitCase):

    def test_delete_merged_into_head_or_upstream(self):
        c1, f1 = self.two_branches()
        self.mg("branch", "done", c1)
        self.mg("switch", "feature")
        res = self.mg("branch", "-d", "done")            # C1 is an ancestor of HEAD (F1)
        self.assertIn(f"Deleted branch done (was {c1[:7]}).", res.out)
        self.mg_fail("rev-parse", "done")
        self.mg("switch", "main")
        # feature (F1) is not in HEAD (C1) but is in its upstream origin/feature.
        self.write_ref("refs/remotes/origin/feature", f1)
        self.mg("config", "branch.feature.remote", "origin")
        self.mg("config", "branch.feature.merge", "refs/heads/feature")
        self.mg("branch", "-d", "feature")
        self.assertEqual(self.ref_files(), {"main"})
        self.assertEqual(self.rev("origin/feature"), f1, "remote-tracking ref must survive")

    def test_delete_current_branch_refused(self):
        c1, c2 = self.linear()
        for flag in ("-d", "-D"):
            with self.subTest(flag=flag):
                res = self.mg_fail("branch", flag, "main")
                self.assertIn("cannot delete the branch 'main' you are on", res.err)
                self.assertIn("main", self.ref_files(), f"'branch {flag} main' deleted the current branch")
                self.assertOnBranch("main")
                self.assertEqual(self.rev("HEAD"), c2)

    def test_delete_unmerged_needs_force_and_missing_branch_errors(self):
        c1, f1 = self.two_branches()
        res = self.mg_fail("branch", "-d", "feature")
        self.assertIn("the branch 'feature' is not fully merged (use -D)", res.err)
        self.assertEqual(self.rev("feature"), f1)
        res = self.mg("branch", "-D", "feature")
        self.assertIn(f"Deleted branch feature (was {f1[:7]}).", res.out)
        self.assertEqual(self.ref_files(), {"main"})
        for flag in ("-d", "-D"):
            with self.subTest(flag=flag):
                res = self.mg_fail("branch", flag, "feature")
                self.assertIn("feature", res.err)
                res = self.mg_fail("branch", flag, "ghost")
                self.assertIn("ghost", res.err)
        self.assertEqual(self.rev("main"), c1)

    def test_delete_while_detached(self):
        c1, c2 = self.linear()
        self.mg("switch", "-c", "newer")
        self.w("a.txt", "3\n")
        c3 = self.commit("three")
        self.mg("switch", "--detach", "main")
        self.assertEqual(self.head(), c2)
        res = self.mg_fail("branch", "-d", "newer")  # c3 is not in HEAD (c2)
        self.assertIn("not fully merged", res.err)
        self.assertEqual(self.rev("newer"), c3)
        self.mg("branch", "-d", "main")             # c2 is HEAD itself
        self.assertEqual(self.ref_files(), {"newer"})
        self.assertEqual(self.head(), c2, "deleting a branch moved the detached HEAD")

    def test_usage_errors(self):
        self.linear()
        for argv in (["branch", "-d"], ["branch", "-D"], ["switch"], ["switch", "a", "b"],
                     ["checkout"], ["switch", "-c"], ["checkout", "-b"]):
            with self.subTest(argv=argv):
                self.mg_fail(*argv, code=2)
        self.assertOnBranch("main")
        self.assertEqual(self.ref_files(), {"main"})


# ===========================================================================
# switch / checkout
# ===========================================================================
class SwitchCheckoutTests(MinigitCase):

    def test_switch_updates_head_worktree_and_index(self):
        c1, f1 = self.two_branches()
        res = self.mg("switch", "feature")
        self.assertIn("Switched to branch 'feature'", res.out)
        self.assertOnBranch("feature")
        self.assertEqual(self.r("diff.txt"), b"feature\n")
        self.assertEqual(self.r("new.txt"), b"feature new\n")
        self.assertFalse(self.exists("gone.txt"))
        self.assertFalse(self.exists("sub/only_main.txt"))
        self.assertFalse(self.exists("sub"), "empty directory 'sub' was not pruned")
        self.assertEqual(self.porcelain(), "", "index/worktree not consistent after switch")
        self.assertIn("On branch feature", self.mg("status").out)
        self.mg("switch", "main")
        self.assertOnBranch("main")
        self.assertEqual(self.r("diff.txt"), b"base\n")
        self.assertEqual(self.r("gone.txt"), b"gone on feature\n")
        self.assertEqual(self.r("sub/only_main.txt"), b"main only\n")
        self.assertFalse(self.exists("new.txt"))
        self.assertEqual(self.porcelain(), "")

    def test_switch_c_and_checkout_b(self):
        c1, c2 = self.linear()
        res = self.mg("switch", "-c", "topic")
        self.assertIn("Switched to a new branch 'topic'", res.out)
        self.assertOnBranch("topic")
        self.assertEqual(self.rev("topic"), c2)
        self.w("a.txt", "topic\n")
        out = self.mg("commit", "-a", "-m", "on topic").out
        self.assertTrue(out.startswith("[topic "), out)
        self.assertEqual(self.rev("main"), c2, "commit on topic moved main")
        res = self.mg("checkout", "-b", "old", c1)
        self.assertIn("Switched to a new branch 'old'", res.out)
        self.assertOnBranch("old")
        self.assertEqual(self.rev("old"), c1)
        self.assertEqual(self.r("a.txt"), b"v1\n")
        self.assertFalse(self.exists("b.txt"))
        self.assertEqual(self.porcelain(), "")

    def test_switch_unknown_or_broken_target(self):
        c1, c2 = self.linear()
        self.mg_fail("switch", "nosuch")
        self.mg_fail("checkout", "nosuch")
        self.assertOnBranch("main")
        self.assertEqual(self.ref_files(), {"main"})
        # A branch whose commit object is missing: clean error, nothing touched.
        self.write_ref("refs/heads/broken", "0123456789abcdef0123456789abcdef01234567")
        self.mg_fail("switch", "broken")
        self.assertOnBranch("main")
        self.assertEqual(self.r("a.txt"), b"v2\n")
        self.assertEqual(self.porcelain(), "")

    def test_switch_requires_a_branch(self):
        # SPEC: "switch B" vs "switch --detach REV"; git: "a branch is expected, got commit".
        c1, c2 = self.linear()
        self.write_ref("refs/remotes/origin/main", c1)
        for target in (c1, c1[:7], "HEAD~1", "origin/main"):
            with self.subTest(target=target):
                if self.head() != "ref: refs/heads/main":
                    self.mg("checkout", "main")  # undo a previous subtest's detach
                res = self.run_mg("switch", target)
                self.assertNoCrash(res)
                self.assertNotEqual(res.code, 0, f"'switch {target}' should need --detach\n{res}")
                self.assertOnBranch("main")
                self.assertEqual(self.r("a.txt"), b"v2\n")

    def test_detach_commit_and_reattach(self):
        c1, c2 = self.linear()
        res = self.mg("switch", "--detach", "HEAD~1")
        self.assertIn(f"HEAD is now at {c1[:7]} first", res.out)
        self.assertEqual(self.head(), c1)
        self.assertEqual(self.r("a.txt"), b"v1\n")
        self.assertFalse(self.exists("b.txt"))
        self.assertIn(f"HEAD detached at {c1[:7]}", self.mg("status").out)
        listing = self.mg("branch").out
        self.assertNotRegex(listing, r"(?m)^\* main$", "detached HEAD still marks main as current")
        self.assertRegex(listing, r"(?m)^  main$")
        self.mg("branch", "fromdetached")
        self.assertEqual(self.rev("fromdetached"), c1)
        self.assertEqual(self.head(), c1, "'branch X' while detached must not attach HEAD")
        self.w("a.txt", "detached\n")
        out = self.mg("commit", "-a", "-m", "on detached").out
        self.assertTrue(out.startswith("[detached HEAD "), out)
        c3 = self.rev("HEAD")
        self.assertEqual(self.head(), c3)
        self.assertEqual(self.rev("main"), c2, "detached commit moved main")
        self.assertEqual(self.rev("fromdetached"), c1, "detached commit moved a branch")
        res = self.mg("checkout", "main")
        self.assertIn("Switched to branch 'main'", res.out)
        self.assertOnBranch("main")
        self.assertEqual(self.r("a.txt"), b"v2\n")
        res = self.mg("checkout", c1[:7])
        self.assertIn(f"HEAD is now at {c1[:7]}", res.out)
        self.assertEqual(self.head(), c1, "checkout <short sha> must store the full sha in HEAD")
        self.mg("switch", "--detach")  # defaults to HEAD
        self.assertEqual(self.head(), c1)

    def test_switch_to_current_branch_is_noop(self):
        c1, c2 = self.linear()
        self.w("a.txt", "dirty\n")
        self.w("u.txt", "untracked\n")
        for argv in (["switch", "main"], ["checkout", "main"]):
            with self.subTest(argv=argv):
                self.mg(*argv)
                self.assertOnBranch("main")
                self.assertEqual(self.rev("HEAD"), c2)
                self.assertEqual(self.r("a.txt"), b"dirty\n")
                self.assertEqual(self.porcelain(), " M a.txt\n?? u.txt\n")

    def test_checkout_name_resolution(self):
        self.w("feature", "v1\n")
        c1 = self.commit("one")
        self.mg("branch", "feature")
        self.w("feature", "v2\n")
        self.commit("two")
        with self.subTest("checkout HEAD stays attached (git never detaches here)"):
            res = self.run_mg("checkout", "HEAD")
            self.assertNoCrash(res)
            self.assertEqual(res.code, 0, str(res))
            self.assertOnBranch("main")
        if self.head() != "ref: refs/heads/main":
            self.mg("checkout", "main")
        with self.subTest("a branch beats a same-named tracked file"):
            self.mg("checkout", "feature")
            self.assertOnBranch("feature")
            self.assertEqual(self.r("feature"), b"v1\n")
            self.assertEqual(self.rev("HEAD"), c1)
            self.assertEqual(self.porcelain(), "")

    def test_checkout_paths_do_not_move_head(self):
        c1, f1 = self.two_branches()
        self.w("common.txt", "scribble\n")
        self.mg("checkout", "--", "common.txt")
        self.assertEqual(self.r("common.txt"), b"common\n")
        self.mg("checkout", "feature", "--", "diff.txt")
        self.assertEqual(self.r("diff.txt"), b"feature\n")
        self.mg("checkout", "feature", "new.txt")
        self.assertEqual(self.r("new.txt"), b"feature new\n")
        self.assertOnBranch("main")
        self.assertEqual(self.rev("HEAD"), c1)
        self.assertEqual(self.porcelain(), "M  diff.txt\nA  new.txt\n")

    def test_bytes_and_names_preserved_across_switches(self):
        self.linear()
        self.mg("switch", "-c", "bin")
        blobs = {
            "crlf.txt": b"line1\r\nline2\r\n",
            "nonl.txt": b"no newline at end",
            "data.bin": bytes(range(256)) * 4 + b"\x00\x00",
            "empty.txt": b"",
            "deep/a/b/c.txt": b"\r\nmixed\nends\r",
            "dïr/naïve été.txt": "café\n".encode("utf-8"),
        }
        for rel, data in blobs.items():
            self.w(rel, data)
        self.commit("bytes")
        self.mg("switch", "main")
        for rel in blobs:
            self.assertFalse(self.exists(rel), rel)
        self.assertFalse(self.exists("deep"), "empty 'deep' directory tree was not pruned")
        self.mg("switch", "bin")
        for rel, data in blobs.items():
            self.assertEqual(self.r(rel), data, rel)
        self.assertEqual(self.porcelain(), "")

    @unittest.skipIf(os.name == "nt", "executable bit is POSIX only")
    def test_executable_bit_follows_branch(self):
        self.linear()
        self.mg("switch", "-c", "exec")
        self.w("run.sh", "#!/bin/sh\necho hi\n")
        os.chmod(self.p("run.sh"), 0o755)
        self.commit("exec")
        self.mg("switch", "main")
        self.assertFalse(self.exists("run.sh"))
        self.mg("switch", "exec")
        self.assertTrue(os.stat(self.p("run.sh")).st_mode & stat.S_IXUSR, "lost the +x bit")
        self.assertEqual(self.porcelain(), "")

    def test_switch_from_subdirectory_that_disappears(self):
        c1, f1 = self.two_branches()
        res = self.run_mg("switch", "feature", cwd=self.p("sub"))
        self.assertNoCrash(res)
        self.assertEqual(res.code, 0, str(res))
        self.assertOnBranch("feature")
        self.assertFalse(self.exists("sub/only_main.txt"))
        self.assertEqual(self.r("diff.txt"), b"feature\n")
        self.assertEqual(self.porcelain(), "")

    def test_dwim_remote_tracking_branch(self):
        c1, c2 = self.linear()
        self.write_ref("refs/remotes/origin/topic", c1)
        res = self.mg("switch", "topic")
        self.assertIn("topic", res.out)
        self.assertOnBranch("topic")
        self.assertEqual(self.rev("refs/heads/topic"), c1)
        self.assertEqual(self.r("a.txt"), b"v1\n")
        self.assertEqual(self.mg("config", "branch.topic.remote").out.strip(), "origin")
        self.assertEqual(self.mg("config", "branch.topic.merge").out.strip(), "refs/heads/topic")
        # checkout DWIM must attach to a new local branch, not detach.
        self.write_ref("refs/remotes/origin/topic2", c2)
        self.mg("checkout", "topic2")
        self.assertOnBranch("topic2")
        self.assertEqual(self.rev("refs/heads/topic2"), c2)
        # Ambiguous: two remotes have the name -> refuse, create nothing.
        self.write_ref("refs/remotes/origin/dup", c1)
        self.write_ref("refs/remotes/upstream/dup", c2)
        self.mg_fail("switch", "dup")
        self.assertOnBranch("topic2")
        self.assertNotIn("dup", self.ref_files())

    @unittest.skipUnless(os.name == "nt", "case-insensitive filesystem only")
    def test_case_only_rename_between_branches_on_windows(self):
        # main tracks README.txt, 'lower' tracks readme.txt. git for Windows
        # (core.ignorecase) switches both ways on a clean tree.
        self.w("README.txt", "upper\n")
        self.w("b.txt", "b\n")
        self.commit("upper")
        self.mg("switch", "-c", "lower")
        self.mg("rm", "--cached", "README.txt")
        os.rename(self.p("README.txt"), self.p("tmp-rename"))
        os.rename(self.p("tmp-rename"), self.p("readme.txt"))
        self.w("readme.txt", "lower\n")
        self.mg("add", "readme.txt")
        self.mg("commit", "-m", "lower")
        self.assertEqual(self.porcelain(), "")
        res = self.run_mg("switch", "main")
        self.assertNoCrash(res)
        self.assertEqual(res.code, 0, f"clean case-only rename blocks the switch\n{res}")
        self.assertOnBranch("main")
        self.assertIn("README.txt", os.listdir(self.repo))
        self.assertEqual(self.r("README.txt"), b"upper\n")
        self.assertEqual(self.porcelain(), "")
        self.mg("switch", "lower")
        self.assertIn("readme.txt", os.listdir(self.repo))
        self.assertEqual(self.r("readme.txt"), b"lower\n")


# ===========================================================================
# protecting uncommitted work
# ===========================================================================
class DirtyWorktreeTests(MinigitCase):

    def test_non_conflicting_local_changes_are_carried(self):
        self.two_branches()
        self.w("common.txt", "edited\n")              # unstaged modification
        self.w("staged.txt", "staged edit\n")
        self.mg("add", "staged.txt")                  # staged modification
        self.w("extra.txt", "brand new\n")
        self.mg("add", "extra.txt")                   # staged new file
        self.delete("keep.txt")                       # unstaged deletion
        self.w("notes.txt", "mine\n")                 # untracked
        self.w("untracked/x/y.txt", "deep\n")         # untracked tree
        self.w("sub/mine.txt", "next to a file feature deletes\n")
        expected = (" M common.txt\nA  extra.txt\n D keep.txt\n?? notes.txt\n"
                    "M  staged.txt\n?? sub/mine.txt\n?? untracked/x/y.txt\n")
        self.assertEqual(self.porcelain(), expected)
        self.mg("switch", "feature")
        self.assertOnBranch("feature")
        self.assertEqual(self.r("diff.txt"), b"feature\n")
        self.assertEqual(self.r("new.txt"), b"feature new\n")
        self.assertFalse(self.exists("sub/only_main.txt"))
        self.assertEqual(self.r("sub/mine.txt"), b"next to a file feature deletes\n",
                         "pruning removed an untracked file")
        self.assertEqual(self.r("common.txt"), b"edited\n")
        self.assertFalse(self.exists("keep.txt"))
        self.assertEqual(self.porcelain(), expected, "local changes not carried to feature")
        self.mg("switch", "main")
        self.assertEqual(self.r("diff.txt"), b"base\n")
        self.assertEqual(self.porcelain(), expected, "local changes not carried back to main")

    def test_conflicting_unstaged_change_refuses_every_form(self):
        c1, f1 = self.two_branches()
        self.w("diff.txt", "mine\n")
        for argv in (["switch", "feature"], ["checkout", "feature"], ["switch", "--detach", "feature"],
                     ["checkout", f1]):
            with self.subTest(argv=argv):
                res = self.mg_fail(*argv)
                self.assertIn(OVERWRITE_HEAD + "\n\tdiff.txt\n" + OVERWRITE_TAIL, res.err)
                self.assertOnBranch("main")
                self.assertEqual(self.r("diff.txt"), b"mine\n")
                self.assertFalse(self.exists("new.txt"))
                self.assertTrue(self.exists("gone.txt"))
                self.assertTrue(self.exists("sub/only_main.txt"))
                self.assertEqual(self.porcelain(), " M diff.txt\n")

    def test_refusal_touches_nothing(self):
        for i in range(1, 7):
            self.w(f"f{i}.txt", f"main {i}\n")
        self.w("z_del.txt", "deleted on feature\n")
        self.commit("base")
        self.mg("switch", "-c", "feature")
        for i in range(1, 7):
            self.w(f"f{i}.txt", f"feature {i}\n")
        self.w("a_new.txt", "added on feature\n")
        self.delete("z_del.txt")
        self.commit("feature")
        self.mg("switch", "main")
        self.w("f4.txt", "dirty\n")
        res = self.mg_fail("switch", "feature")
        self.assertIn("\tf4.txt", res.err)
        self.assertNotIn("\tf1.txt", res.err, "clean files listed as conflicts")
        for i in (1, 2, 3, 5, 6):
            self.assertEqual(self.r(f"f{i}.txt"), f"main {i}\n".encode(), f"f{i}.txt changed")
        self.assertFalse(self.exists("a_new.txt"), "a_new.txt written despite the refusal")
        self.assertTrue(self.exists("z_del.txt"), "z_del.txt deleted despite the refusal")
        self.assertEqual(self.porcelain(), " M f4.txt\n", "index changed despite the refusal")
        self.assertOnBranch("main")

    def test_conflicting_staged_change_refused(self):
        self.two_branches()
        self.w("diff.txt", "staged\n")
        self.mg("add", "diff.txt")
        self.mg_fail("switch", "feature")
        self.assertEqual(self.porcelain(), "M  diff.txt\n")
        # Worktree reverted to HEAD's bytes but the index still differs: still refused.
        self.w("diff.txt", "base\n")
        self.assertEqual(self.porcelain(), "MM diff.txt\n")
        res = self.mg_fail("switch", "feature")
        self.assertIn("diff.txt", res.err)
        self.assertEqual(self.porcelain(), "MM diff.txt\n", "staged content was clobbered")
        self.assertOnBranch("main")

    def test_changes_matching_the_target_are_allowed(self):
        self.two_branches()
        self.w("diff.txt", "feature\n")
        self.mg("add", "diff.txt")                 # index == target version
        self.w("new.txt", "feature new\n")          # untracked, identical to target
        self.mg("switch", "feature")
        self.assertOnBranch("feature")
        self.assertEqual(self.porcelain(), "")

    def test_each_kind_of_conflict_refused_and_listed(self):
        cases = {
            "untracked file in the way": {"new.txt": "my precious untracked work\n"},
            "modified file the target deletes": {"gone.txt": "edited but the target deletes me\n"},
            "all kinds at once": {"diff.txt": "mine\n", "new.txt": "untracked in the way\n",
                                  "gone.txt": "edited\n"},
        }
        for label, edits in cases.items():
            with self.subTest(label):
                self.two_branches()                    # fresh copy for every case
                for rel, data in edits.items():
                    self.w(rel, data)
                res = self.mg_fail("switch", "feature")
                self.assertIn(OVERWRITE_HEAD, res.err)
                self.assertIn(OVERWRITE_TAIL, res.err)
                for rel, data in edits.items():
                    self.assertIn(f"\t{rel}\n", res.err, f"{rel} not listed")
                    self.assertEqual(self.r(rel), data.encode(), f"{rel} was clobbered")
                self.assertOnBranch("main")
                if "diff.txt" not in edits:
                    self.assertEqual(self.r("diff.txt"), b"base\n")

    def test_create_and_switch_refusal_leaves_no_branch(self):
        c1, f1 = self.two_branches()
        self.w("diff.txt", "mine\n")
        self.mg_fail("switch", "-c", "newb", "feature")
        self.mg_fail("checkout", "-b", "newb2", f1)
        self.assertEqual(self.ref_files(), {"main", "feature"},
                         "a refused 'switch -c B START' still created B")
        self.assertOnBranch("main")
        self.assertEqual(self.r("diff.txt"), b"mine\n")

    def test_held_locks_refuse_cleanly(self):
        self.two_branches()
        gitdir = os.path.join(self.repo, ".minigit")
        with self.subTest(lock="index.lock"):
            lock = os.path.join(gitdir, "index.lock")
            open(lock, "wb").close()
            res = self.mg_fail("switch", "feature")
            self.assertIn("unable to lock", res.err)
            self.assertTrue(os.path.exists(lock), "someone else's index.lock was deleted")
            self.assertOnBranch("main")
            self.assertEqual(self.r("diff.txt"), b"base\n")
            self.assertFalse(self.exists("new.txt"))
            os.remove(lock)
            self.assertEqual(self.porcelain(), "")
        with self.subTest(lock="refs/heads/topic.lock"):
            lock = os.path.join(gitdir, "refs", "heads", "topic.lock")
            open(lock, "wb").close()
            for argv in (["branch", "topic"], ["switch", "-c", "topic"]):
                res = self.mg_fail(*argv)
                self.assertIn("unable to lock", res.err)
            self.assertTrue(os.path.exists(lock), "someone else's ref lock was deleted")
            self.assertEqual(self.ref_files(), {"main", "feature", "topic.lock"})
            self.assertOnBranch("main")

    def test_racy_same_size_edit_is_detected(self):
        self.two_branches()
        self.mg("switch", "feature")
        path = self.p("diff.txt")
        st = os.stat(path)
        self.w("diff.txt", "FEATURE\n")               # same size as "feature\n"
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.porcelain(), " M diff.txt\n", "racy edit not seen by status")
        res = self.mg_fail("switch", "main")
        self.assertIn("\tdiff.txt", res.err)
        self.assertEqual(self.r("diff.txt"), b"FEATURE\n", "racy edit was silently overwritten")
        self.assertOnBranch("feature")

    def test_switch_refused_while_merging(self):
        self.w("m.txt", "base\n")
        self.commit("base")
        self.mg("switch", "-c", "other")
        self.w("m.txt", "other\n")
        self.commit("other")
        self.mg("switch", "main")
        self.w("m.txt", "main\n")
        self.commit("main")
        self.mg_fail("merge", "other")
        self.assertTrue(os.path.exists(os.path.join(self.repo, ".minigit", "MERGE_HEAD")))
        for argv in (["switch", "other"], ["checkout", "other"], ["switch", "-c", "newb"],
                     ["checkout", "-b", "newb2"], ["switch", "--detach", "other"]):
            with self.subTest(argv=argv):
                res = self.mg_fail(*argv)
                self.assertIn(MERGE_BLOCK, res.err)
                self.assertOnBranch("main")
        self.assertEqual(self.ref_files(), {"main", "other"})
        self.assertIn(b"<<<<<<< HEAD", self.r("m.txt"), "conflict markers lost")
        self.mg("merge", "--abort")
        self.mg("switch", "other")
        self.assertEqual(self.r("m.txt"), b"other\n")


# ===========================================================================
# file <-> directory transitions
# ===========================================================================
class DirFileClashTests(MinigitCase):

    def setUp(self):
        super().setUp()

        def build():
            # main: x is a file.  dirside: x/y.txt.  nox: no x at all.
            self.w("a.txt", "a\n")
            self.w("x", "file\n")
            self.commit("x file")
            self.mg("branch", "nox")
            self.mg("switch", "-c", "dirside")
            self.delete("x")
            self.w("x/y.txt", "nested\n")
            self.commit("x dir")
            self.mg("switch", "nox")
            self.delete("x")
            self.commit("no x")
            self.mg("switch", "main")
            self.assertEqual(self.r("x"), b"file\n", "fixture failed")
        self.from_template("dirfile", build)

    def test_file_dir_swap_both_ways(self):
        self.mg("switch", "dirside")
        self.assertTrue(os.path.isdir(self.p("x")))
        self.assertEqual(self.r("x/y.txt"), b"nested\n")
        self.assertEqual(self.porcelain(), "")
        self.mg("switch", "main")
        self.assertTrue(os.path.isfile(self.p("x")))
        self.assertEqual(self.r("x"), b"file\n")
        self.assertEqual(self.porcelain(), "")

    def test_modified_file_blocks_file_to_dir(self):
        self.w("x", "edited file\n")
        res = self.mg_fail("switch", "dirside")
        self.assertIn("\tx", res.err)
        self.assertEqual(self.r("x"), b"edited file\n")
        self.assertOnBranch("main")

    def test_untracked_file_in_directory_blocks_dir_to_file(self):
        self.mg("switch", "dirside")
        self.w("x/extra.txt", "untracked\n")
        self.mg_fail("switch", "main")
        self.assertEqual(self.r("x/extra.txt"), b"untracked\n")
        self.assertEqual(self.r("x/y.txt"), b"nested\n")
        self.assertOnBranch("dirside")
        os.remove(self.p("x/extra.txt"))
        self.mg("switch", "main")
        self.assertEqual(self.r("x"), b"file\n")

    def test_untracked_file_blocks_needed_directory(self):
        self.mg("switch", "nox")
        self.assertFalse(self.exists("x"))
        self.w("x", "untracked file where a dir is needed\n")
        self.mg_fail("switch", "dirside")
        self.assertEqual(self.r("x"), b"untracked file where a dir is needed\n")
        self.assertOnBranch("nox")
        self.assertEqual(self.porcelain(), "?? x\n")


if __name__ == "__main__":
    unittest.main()

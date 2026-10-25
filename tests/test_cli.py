"""Unit tests for minigit.cli (owner: M7 cli).

Every library module is replaced by a small fake installed in ``sys.modules``
so the CLI can be tested in isolation, against the SPEC.md signatures, no
matter which real modules exist. ``minigit.cli`` imports lazily through
``importlib.import_module`` so the fakes are picked up at call time.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from minigit import cli

SHA = "0123456789abcdef0123456789abcdef01234567"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def fake_errors() -> types.ModuleType:
    mod = types.ModuleType("minigit.errors")

    class MinigitError(Exception):
        exit_code = 1

        def __init__(self, message, *, hint=None):
            super().__init__(message)
            self.message = message
            self.hint = hint

    class UsageError(MinigitError):
        exit_code = 2

    class NotARepositoryError(MinigitError):
        pass

    class RevisionError(MinigitError):
        pass

    class NothingToCommit(MinigitError):
        pass

    class CheckoutConflictError(MinigitError):
        def __init__(self, message, *, paths, hint=None):
            super().__init__(message, hint=hint)
            self.paths = paths

    class RemoteError(MinigitError):
        def __init__(self, message, *, status=None, code=None, payload=None, hint=None):
            super().__init__(message, hint=hint)
            self.status, self.code, self.payload = status, code, payload

    class AuthError(RemoteError):
        pass

    class PermissionDeniedError(RemoteError):
        pass

    class PushRejectedError(RemoteError):
        def __init__(self, message, *, results, url, **kw):
            super().__init__(message, **kw)
            self.results, self.url = results, url

    for cls in (MinigitError, UsageError, NotARepositoryError, RevisionError, NothingToCommit,
                CheckoutConflictError, RemoteError, AuthError, PermissionDeniedError,
                PushRejectedError):
        setattr(mod, cls.__name__, cls)
    return mod


class FakeRefs:
    def __init__(self, branch="main", remotes=None):
        self.branch = branch
        self.remotes = remotes or {}

    def current_branch(self):
        return self.branch

    def list(self, prefix="refs/"):
        return {k: v for k, v in self.remotes.items() if k.startswith(prefix)}


class FakeRepo:
    def __init__(self, gitdir="/x/.minigit", branch="main", remotes=None):
        self.gitdir = gitdir
        self.worktree = os.path.dirname(gitdir)
        self.refs = FakeRefs(branch, remotes)


def mod(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType("minigit." + name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class Outcome:
    def __init__(self, kind, messages, commit=SHA):
        self.kind, self.messages, self.commit = kind, messages, commit
        self.old_head, self.conflicts = None, []


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------

class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.E = fake_errors()
        self.repo = FakeRepo()
        self.ops = mod(
            "ops",
            init=Mock(return_value="Initialized empty minigit repository in X/.minigit"),
            config_cmd=Mock(return_value=None),
            add=Mock(return_value=["a.txt"]),
            rm=Mock(return_value=["rm 'a.txt'"]),
            commit=Mock(return_value=(SHA, "[main 0123456] subject")),
            branch_list=Mock(return_value=["items"]),
            format_branch_list=Mock(return_value="* main\n  feature"),
            branch_create=Mock(return_value=SHA),
            branch_delete=Mock(return_value="Deleted branch f (was 0123456)."),
            switch=Mock(return_value="Switched to branch 'f'"),
            reset=Mock(return_value=""),
            reset_paths=Mock(return_value=["a.txt"]),
            restore=Mock(return_value=["a.txt"]),
            get_upstream=Mock(return_value=("upstream", "refs/heads/main")),
            cat_file=Mock(return_value=b"blob content"),
            ls_files=Mock(return_value="a.txt\nb.txt"),
            hash_object_cmd=Mock(return_value=SHA),
        )
        self.repo_mod = mod("repo", Repository=types.SimpleNamespace(
            discover=Mock(return_value=self.repo)))
        self.status = mod(
            "status",
            compute_status=Mock(return_value="ST"),
            format_status_long=Mock(return_value="On branch main\n\nnothing to commit, working tree clean"),
            format_status_porcelain=Mock(return_value="A  a.txt\n"),
        )
        self.diff = mod("diff", diff_command=Mock(return_value=b"diff --git a/a b/a\n"))
        self.history = mod(
            "history",
            log_entries=Mock(return_value=[("sha", "commit")]),
            format_log=Mock(return_value="commit abc\n"),
            show=Mock(return_value=b"commit abc\n\ndiff\n"),
        )
        self.merge = mod(
            "merge",
            merge=Mock(return_value=Outcome("fast-forward", ["Updating 1..2", "Fast-forward"])),
            merge_abort=Mock(return_value=None),
        )
        self.remote = mod(
            "remote",
            remote_add=Mock(), remote_remove=Mock(),
            remote_list=Mock(return_value={"origin": "http://h:1/p"}),
            remote_url=Mock(return_value="http://h:1/p"),
            clone=Mock(return_value=(self.repo, ["Cloning into 'p'...", "warning: You appear to have cloned an empty repository."])),
            fetch=Mock(return_value=["change"]),
            format_fetch=Mock(return_value="From http://h:1/p\n * [new branch]      main -> origin/main"),
            pull=Mock(return_value=(["change"], Outcome("merged", ["Merge made by the 'three-way' strategy."]))),
            push=Mock(return_value=["To http://h:1/p", "   1..2  main -> main"]),
            format_push_rejection=Mock(return_value="To http://h:1/p\n ! [rejected]        main -> main (non-fast-forward)\nhint: Run 'minigit pull' first, then push again (pull first)."),
            login=Mock(),
        )
        self.transport = mod("transport", split_url=Mock(return_value=("http://h:1", ("u", "t"))))
        self.revparse = mod(
            "revparse",
            resolve_object=Mock(return_value=SHA),
            resolve_rev=Mock(return_value=SHA),
            resolve_local_branch=Mock(return_value=None),
            abbrev=Mock(side_effect=lambda s, n=7: s[:n]),
        )
        self.auth = mod(
            "auth",
            server_init=Mock(), add_user=Mock(return_value="TOKEN123"),
            create_repo=Mock(return_value="/root/repos/proj"),
            list_server=Mock(return_value="users:\n  alice\nrepos:\n  proj"),
        )
        self.server = mod("server", serve=Mock(return_value=0))
        self.pack = mod("pack", fsck=Mock(return_value=[]))
        mods = {
            "minigit.errors": self.E, "minigit.ops": self.ops, "minigit.repo": self.repo_mod,
            "minigit.status": self.status, "minigit.diff": self.diff, "minigit.history": self.history,
            "minigit.merge": self.merge, "minigit.remote": self.remote, "minigit.transport": self.transport,
            "minigit.revparse": self.revparse, "minigit.auth": self.auth, "minigit.server": self.server,
            "minigit.pack": self.pack,
        }
        patcher = patch.dict(sys.modules, mods)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.realpath(self.tmp.name)

    def cli(self, *args, env=None):
        """Run main() with -C <tmp>; return (rc, stdout, stderr)."""
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        argv = ["-C", self.cwd, *args]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with patch.dict(os.environ, env or {}, clear=False):
                rc = cli.main(argv)
        out.flush()
        err.flush()
        return rc, out.buffer.getvalue().decode("utf-8"), err.buffer.getvalue().decode("utf-8")

    def ok(self, *args):
        rc, out, err = self.cli(*args)
        self.assertEqual(rc, 0, f"stdout={out!r} stderr={err!r}")
        return out

    def touch(self, name, data=b"x"):
        path = os.path.join(self.cwd, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path


# ---------------------------------------------------------------------------
# Parser, global flags, errors
# ---------------------------------------------------------------------------

class ParserTests(CliTestCase):
    def test_build_parser_prog(self):
        self.assertEqual(cli.build_parser().prog, "minigit")

    def test_version(self):
        rc, out, _ = self.cli("--version")
        self.assertEqual(rc, 0)
        self.assertIn("minigit 0.1.0", out)

    def test_help(self):
        rc, out, _ = self.cli("--help")
        self.assertEqual(rc, 0)
        self.assertIn("usage: minigit", out)
        for name in ("init", "add", "commit", "merge", "push", "serve", "server", "fsck"):
            self.assertIn(name, out)

    def test_no_args_prints_help_exit_2(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main([])
        self.assertEqual(rc, 2)
        self.assertIn("usage: minigit", out.getvalue())

    def test_every_command_has_help(self):
        for name in cli._COMMANDS:
            rc, out, err = self.cli(name, "--help")
            self.assertEqual(rc, 0, name)
            self.assertIn(f"usage: minigit {name}", out)

    def test_unknown_command_is_usage_error(self):
        rc, _, err = self.cli("frobnicate")
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)
        self.assertIn("invalid choice", err)

    def test_missing_required_argument(self):
        rc, _, err = self.cli("clone")
        self.assertEqual(rc, 2)
        self.assertIn("usage: minigit clone", err)
        self.assertIn("error: the following arguments are required", err)

    def test_parser_error_raises_usage_error(self):
        with self.assertRaises(self.E.UsageError):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.build_parser().parse_args(["--bogus"])

    def test_bad_C_dir(self):
        rc, _, err = self.cli("-C", "does-not-exist", "status")
        self.assertEqual(rc, 1)
        self.assertIn("error: cannot change to 'does-not-exist'", err)

    def test_C_sets_cwd_for_discovery(self):
        sub = os.path.join(self.cwd, "sub")
        os.makedirs(sub)
        self.ok("-C", "sub", "status")
        self.repo_mod.Repository.discover.assert_called_once_with(sub)

    def test_minigit_error_output_and_hint(self):
        self.status.compute_status.side_effect = self.E.MinigitError("boom", hint="try this")
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "error: boom\nhint: try this\n")

    def test_usage_error_from_library_exits_2(self):
        self.ops.commit.side_effect = self.E.UsageError("commit message required: use -m")
        rc, _, err = self.cli("commit")
        self.assertEqual(rc, 2)
        self.assertIn("error: commit message required", err)

    def test_not_a_repository(self):
        self.repo_mod.Repository.discover.side_effect = self.E.NotARepositoryError(
            "not a minigit repository (or any parent): /x")
        rc, _, err = self.cli("status")
        self.assertEqual(rc, 1)
        self.assertIn("error: not a minigit repository", err)

    def test_internal_error(self):
        self.status.compute_status.side_effect = RuntimeError("kaboom")
        rc, _, err = self.cli("status")
        self.assertEqual(rc, 1)
        self.assertIn("fatal: internal error: kaboom", err)
        self.assertNotIn("Traceback", err)

    def test_internal_error_debug_traceback(self):
        self.status.compute_status.side_effect = RuntimeError("kaboom")
        rc, _, err = self.cli("status", env={"MINIGIT_DEBUG": "1"})
        self.assertEqual(rc, 1)
        self.assertIn("Traceback", err)
        self.assertIn("fatal: internal error: kaboom", err)

    def test_keyboard_interrupt(self):
        self.status.compute_status.side_effect = KeyboardInterrupt()
        rc, _, _ = self.cli("status")
        self.assertEqual(rc, 130)

    def test_main_uses_sys_argv_by_default(self):
        with patch.object(sys, "argv", ["minigit", "--version"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(), 0)

    def test_output_falls_back_without_buffer(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["-C", self.cwd, "init"])
        self.assertEqual(rc, 0)
        self.assertIn("Initialized", out.getvalue())


# ---------------------------------------------------------------------------
# Repository and working tree commands
# ---------------------------------------------------------------------------

class RepoCommandTests(CliTestCase):
    def test_init_default(self):
        out = self.ok("init")
        self.ops.init.assert_called_once_with(self.cwd, bare=False, initial_branch="main")
        self.assertIn("Initialized", out)

    def test_init_bare_branch_dir(self):
        self.ok("init", "--bare", "-b", "trunk", "proj")
        self.ops.init.assert_called_once_with(os.path.join(self.cwd, "proj"), bare=True,
                                              initial_branch="trunk")

    def test_config_set_get_list_unset(self):
        self.ok("config", "user.name", "Alice")
        self.ops.config_cmd.assert_called_with(self.repo, "user.name", "Alice", global_=False,
                                               list_=False, unset=False)
        self.ops.config_cmd.return_value = "Alice"
        self.assertEqual(self.ok("config", "user.name"), "Alice\n")
        self.ops.config_cmd.return_value = "user.name=Alice"
        self.assertEqual(self.ok("config", "--list"), "user.name=Alice\n")
        self.ok("config", "--global", "--unset", "user.name")
        self.ops.config_cmd.assert_called_with(self.repo, "user.name", None, global_=True,
                                               list_=False, unset=True)

    def test_config_requires_key(self):
        rc, _, err = self.cli("config")
        self.assertEqual(rc, 2)
        self.assertIn("key is required", err)

    def test_config_global_outside_repo(self):
        self.repo_mod.Repository.discover.side_effect = self.E.NotARepositoryError("no")
        self.ok("config", "--global", "user.name", "Alice")
        self.ops.config_cmd.assert_called_with(None, "user.name", "Alice", global_=True,
                                               list_=False, unset=False)
        rc, _, err = self.cli("config", "user.name", "Alice")
        self.assertEqual(rc, 1)
        self.assertIn("error: no", err)

    def test_add(self):
        self.ok("add", "a.txt", "dir")
        self.ops.add.assert_called_once_with(self.repo, ["a.txt", "dir"], self.cwd, force=False)
        self.ok("add", "-f", "--", "x.log")
        self.ops.add.assert_called_with(self.repo, ["x.log"], self.cwd, force=True)

    def test_add_nothing(self):
        rc, _, err = self.cli("add")
        self.assertEqual(rc, 2)
        self.assertIn("nothing specified", err)

    def test_add_error_message(self):
        self.ops.add.side_effect = self.E.MinigitError("pathspec 'nope' did not match any files")
        rc, _, err = self.cli("add", "nope")
        self.assertEqual(rc, 1)
        self.assertEqual(err, "error: pathspec 'nope' did not match any files\n")

    def test_rm(self):
        out = self.ok("rm", "--cached", "-r", "-f", "a.txt")
        self.ops.rm.assert_called_once_with(self.repo, ["a.txt"], self.cwd, cached=True,
                                            recursive=True, force=True)
        self.assertEqual(out, "rm 'a.txt'\n")
        rc, _, _ = self.cli("rm")
        self.assertEqual(rc, 2)

    def test_status_long_and_porcelain(self):
        self.assertIn("On branch main", self.ok("status"))
        self.assertEqual(self.ok("status", "--porcelain"), "A  a.txt\n")
        self.assertEqual(self.ok("status", "-s"), "A  a.txt\n")
        self.status.compute_status.assert_called_with(self.repo)

    def test_diff_forms(self):
        self.assertEqual(self.ok("diff"), "diff --git a/a b/a\n")
        self.diff.diff_command.assert_called_with(self.repo, self.cwd, staged=False, revs=[], pathspecs=[])
        self.ok("diff", "--staged")
        self.diff.diff_command.assert_called_with(self.repo, self.cwd, staged=True, revs=[], pathspecs=[])
        self.ok("diff", "--cached", "--", "a.txt")
        self.diff.diff_command.assert_called_with(self.repo, self.cwd, staged=True, revs=[], pathspecs=["a.txt"])
        self.ok("diff", "HEAD~1", "HEAD", "--", "a", "b")
        self.diff.diff_command.assert_called_with(self.repo, self.cwd, staged=False,
                                                  revs=["HEAD~1", "HEAD"], pathspecs=["a", "b"])
        self.ok("diff", "main..feature")
        self.diff.diff_command.assert_called_with(self.repo, self.cwd, staged=False,
                                                  revs=["main..feature"], pathspecs=[])

    def test_diff_path_without_dashdash(self):
        self.touch("a.txt")
        self.revparse.resolve_rev.side_effect = lambda repo, s: (
            SHA if s in ("HEAD", "main") else (_ for _ in ()).throw(self.E.RevisionError("unknown revision")))
        self.ok("diff", "HEAD", "a.txt")
        self.diff.diff_command.assert_called_with(self.repo, self.cwd, staged=False, revs=["HEAD"],
                                                  pathspecs=["a.txt"])
        self.ok("diff", "a.txt")
        self.diff.diff_command.assert_called_with(self.repo, self.cwd, staged=False, revs=[],
                                                  pathspecs=["a.txt"])

    def test_diff_too_many_revs(self):
        rc, _, err = self.cli("diff", "a", "b", "c", "--")
        self.assertEqual(rc, 2)
        self.assertIn("at most two revisions", err)

    def test_diff_binary_output(self):
        self.diff.diff_command.return_value = b"\xff\x00raw"
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with contextlib.redirect_stdout(out):
            cli.main(["-C", self.cwd, "diff"])
        out.flush()
        self.assertEqual(out.buffer.getvalue(), b"\xff\x00raw")

    def test_commit(self):
        out = self.ok("commit", "-m", "one", "-m", "two", "-a", "--allow-empty")
        self.ops.commit.assert_called_once_with(self.repo, ["one", "two"], all_=True, allow_empty=True)
        self.assertEqual(out, "[main 0123456] subject\n")

    def test_commit_nothing_to_commit(self):
        self.ops.commit.side_effect = self.E.NothingToCommit("nothing to commit, working tree clean")
        rc, _, err = self.cli("commit", "-m", "x")
        self.assertEqual(rc, 1)
        self.assertIn("error: nothing to commit, working tree clean", err)

    def test_log(self):
        self.assertEqual(self.ok("log"), "commit abc\n")
        self.history.log_entries.assert_called_with(self.repo, None, None, all_refs=False)
        self.history.format_log.assert_called_with(self.repo, [("sha", "commit")], False)
        self.ok("log", "--oneline", "-n", "3", "--all", "main", "feature")
        self.history.log_entries.assert_called_with(self.repo, ["main", "feature"], 3, all_refs=True)
        self.history.format_log.assert_called_with(self.repo, [("sha", "commit")], True)
        self.ok("log", "--max-count=1")
        self.history.log_entries.assert_called_with(self.repo, None, 1, all_refs=False)

    def test_log_unborn(self):
        self.history.log_entries.side_effect = self.E.MinigitError(
            "your current branch 'main' does not have any commits yet")
        rc, _, err = self.cli("log")
        self.assertEqual(rc, 1)
        self.assertIn("does not have any commits yet", err)

    def test_show(self):
        self.assertEqual(self.ok("show"), "commit abc\n\ndiff\n")
        self.history.show.assert_called_with(self.repo, "HEAD")
        self.ok("show", "HEAD:a.txt")
        self.history.show.assert_called_with(self.repo, "HEAD:a.txt")


# ---------------------------------------------------------------------------
# Branching
# ---------------------------------------------------------------------------

class BranchCommandTests(CliTestCase):
    def test_branch_list(self):
        self.assertEqual(self.ok("branch"), "* main\n  feature\n")
        self.ops.branch_list.assert_called_with(self.repo, remotes=False, all_=False)
        self.ops.format_branch_list.assert_called_with(["items"], False)
        self.ok("branch", "-v", "-r")
        self.ops.branch_list.assert_called_with(self.repo, remotes=True, all_=False)
        self.ops.format_branch_list.assert_called_with(["items"], True)
        self.ok("branch", "-a")
        self.ops.branch_list.assert_called_with(self.repo, remotes=False, all_=True)

    def test_branch_create(self):
        self.assertEqual(self.ok("branch", "feature"), "")
        self.ops.branch_create.assert_called_with(self.repo, "feature", None)
        self.ok("branch", "f2", "HEAD~1")
        self.ops.branch_create.assert_called_with(self.repo, "f2", "HEAD~1")

    def test_branch_delete(self):
        self.assertEqual(self.ok("branch", "-d", "f"), "Deleted branch f (was 0123456).\n")
        self.ops.branch_delete.assert_called_with(self.repo, "f", force=False)
        self.ok("branch", "-D", "f")
        self.ops.branch_delete.assert_called_with(self.repo, "f", force=True)
        rc, _, err = self.cli("branch", "-d")
        self.assertEqual(rc, 2)
        self.assertIn("branch name is required", err)

    def test_branch_errors(self):
        self.ops.branch_delete.side_effect = self.E.MinigitError("the branch 'f' is not fully merged (use -D)")
        rc, _, err = self.cli("branch", "-d", "f")
        self.assertEqual(rc, 1)
        self.assertIn("not fully merged", err)

    def test_switch(self):
        self.assertEqual(self.ok("switch", "f"), "Switched to branch 'f'\n")
        self.ops.switch.assert_called_with(self.repo, "f")
        self.ok("switch", "-c", "new")
        self.ops.switch.assert_called_with(self.repo, None, create="new", start=None)
        self.ok("switch", "-c", "new", "main")
        self.ops.switch.assert_called_with(self.repo, None, create="new", start="main")
        self.ok("switch", "--detach", "abc1234")
        self.ops.switch.assert_called_with(self.repo, "abc1234", detach=True)
        self.ok("switch", "--detach")
        self.ops.switch.assert_called_with(self.repo, "HEAD", detach=True)

    def test_switch_usage(self):
        self.assertEqual(self.cli("switch")[0], 2)
        self.assertEqual(self.cli("switch", "a", "b")[0], 2)
        self.assertEqual(self.cli("switch", "-c", "x", "a", "b")[0], 2)

    def test_switch_refusal(self):
        self.ops.switch.side_effect = self.E.CheckoutConflictError(
            "Your local changes to the following files would be overwritten by checkout:\n\ta.txt\n"
            "Please commit your changes or restore them before you switch branches.", paths=["a.txt"])
        rc, _, err = self.cli("switch", "f")
        self.assertEqual(rc, 1)
        self.assertIn("would be overwritten by checkout", err)
        self.assertIn("\ta.txt", err)

    def test_checkout_branch(self):
        self.revparse.resolve_local_branch.return_value = "refs/heads/f"
        self.ok("checkout", "f")
        self.ops.switch.assert_called_with(self.repo, "f")

    def test_checkout_dwim_remote_branch(self):
        self.repo.refs.remotes = {"refs/remotes/origin/f": SHA}
        self.ok("checkout", "f")
        self.ops.switch.assert_called_with(self.repo, "f")

    def test_checkout_detaches_non_branch(self):
        self.ok("checkout", "abc1234")
        self.ops.switch.assert_called_with(self.repo, "abc1234", detach=True)

    def test_checkout_create(self):
        self.ok("checkout", "-b", "new")
        self.ops.switch.assert_called_with(self.repo, None, create="new", start=None)
        self.ok("checkout", "-b", "new", "main")
        self.ops.switch.assert_called_with(self.repo, None, create="new", start="main")
        self.assertEqual(self.cli("checkout", "-b", "new", "a", "b")[0], 2)

    def test_checkout_paths(self):
        self.ok("checkout", "--", "a.txt", "b.txt")
        self.ops.restore.assert_called_with(self.repo, ["a.txt", "b.txt"], self.cwd)
        self.ok("checkout", "HEAD~1", "--", "a.txt")
        self.ops.restore.assert_called_with(self.repo, ["a.txt"], self.cwd, staged=True, worktree=True,
                                            source="HEAD~1")
        self.ok("checkout", "HEAD", "a.txt")
        self.ops.restore.assert_called_with(self.repo, ["a.txt"], self.cwd, staged=True, worktree=True,
                                            source="HEAD")

    def test_checkout_existing_path_without_dashdash(self):
        self.touch("a.txt")
        self.revparse.resolve_rev.side_effect = self.E.RevisionError("unknown revision 'a.txt'")
        self.ok("checkout", "a.txt")
        self.ops.restore.assert_called_with(self.repo, ["a.txt"], self.cwd)

    def test_checkout_unknown_falls_to_switch(self):
        self.revparse.resolve_rev.side_effect = self.E.RevisionError("unknown revision 'zzz'")
        self.ops.switch.side_effect = self.E.MinigitError("invalid reference: zzz")
        rc, _, err = self.cli("checkout", "zzz")
        self.assertEqual(rc, 1)
        self.assertIn("invalid reference", err)

    def test_checkout_usage(self):
        self.assertEqual(self.cli("checkout")[0], 2)
        self.assertEqual(self.cli("checkout", "a", "b", "--", "c")[0], 2)
        self.assertEqual(self.cli("checkout", "a", "--")[0], 2)

    def test_merge_fast_forward(self):
        out = self.ok("merge", "feature")
        self.merge.merge.assert_called_with(self.repo, "feature", message=None, no_ff=False)
        self.assertEqual(out, "Updating 1..2\nFast-forward\n")

    def test_merge_options(self):
        self.merge.merge.return_value = Outcome("merged", ["Merge made by the 'three-way' strategy."])
        self.ok("merge", "--no-ff", "-m", "msg", "feature")
        self.merge.merge.assert_called_with(self.repo, "feature", message="msg", no_ff=True)

    def test_merge_up_to_date_exit_0(self):
        self.merge.merge.return_value = Outcome("up-to-date", ["Already up to date."])
        self.assertEqual(self.ok("merge", "feature"), "Already up to date.\n")

    def test_merge_conflict_exit_1_stdout(self):
        self.merge.merge.return_value = Outcome(
            "conflict", ["CONFLICT (content): Merge conflict in a.txt",
                         "Automatic merge failed; fix conflicts and then commit the result."])
        rc, out, err = self.cli("merge", "feature")
        self.assertEqual(rc, 1)
        self.assertIn("CONFLICT (content): Merge conflict in a.txt\n", out)
        self.assertIn("Automatic merge failed", out)
        self.assertEqual(err, "")

    def test_merge_abort(self):
        self.ok("merge", "--abort")
        self.merge.merge_abort.assert_called_once_with(self.repo)
        self.assertEqual(self.cli("merge", "--abort", "x")[0], 2)
        self.assertEqual(self.cli("merge")[0], 2)

    def test_merge_error(self):
        self.merge.merge.side_effect = self.E.MinigitError("refusing to merge unrelated histories")
        rc, _, err = self.cli("merge", "other")
        self.assertEqual(rc, 1)
        self.assertIn("unrelated histories", err)

    def test_reset_modes(self):
        self.assertEqual(self.ok("reset"), "")
        self.ops.reset.assert_called_with(self.repo, "HEAD", "mixed")
        self.ok("reset", "--soft", "HEAD~1")
        self.ops.reset.assert_called_with(self.repo, "HEAD~1", "soft")
        self.ops.reset.return_value = "HEAD is now at 0123456 subject"
        self.assertEqual(self.ok("reset", "--hard"), "HEAD is now at 0123456 subject\n")
        self.ops.reset.assert_called_with(self.repo, "HEAD", "hard")
        self.ok("reset", "--mixed", "main")
        self.ops.reset.assert_called_with(self.repo, "main", "mixed")

    def test_reset_paths(self):
        self.ok("reset", "--", "a.txt")
        self.ops.reset_paths.assert_called_with(self.repo, "HEAD", ["a.txt"], self.cwd)
        self.ok("reset", "HEAD~1", "--", "a.txt", "b.txt")
        self.ops.reset_paths.assert_called_with(self.repo, "HEAD~1", ["a.txt", "b.txt"], self.cwd)
        self.ok("reset", "HEAD", "a.txt")
        self.ops.reset_paths.assert_called_with(self.repo, "HEAD", ["a.txt"], self.cwd)
        self.touch("c.txt")
        self.revparse.resolve_rev.side_effect = self.E.RevisionError("unknown revision 'c.txt'")
        self.ok("reset", "c.txt")
        self.ops.reset_paths.assert_called_with(self.repo, "HEAD", ["c.txt"], self.cwd)

    def test_reset_usage(self):
        self.assertEqual(self.cli("reset", "--hard", "--", "a")[0], 2)
        self.assertEqual(self.cli("reset", "a", "b", "--", "c")[0], 2)
        self.assertEqual(self.cli("reset", "--soft", "--hard")[0], 2)

    def test_restore(self):
        self.ok("restore", "a.txt")
        self.ops.restore.assert_called_with(self.repo, ["a.txt"], self.cwd, staged=False, worktree=None,
                                            source=None)
        self.ok("restore", "--staged", "--worktree", "--source", "HEAD~1", "--", "a.txt", "b")
        self.ops.restore.assert_called_with(self.repo, ["a.txt", "b"], self.cwd, staged=True,
                                            worktree=True, source="HEAD~1")
        self.ok("restore", "-s", "main", "x")
        self.ops.restore.assert_called_with(self.repo, ["x"], self.cwd, staged=False, worktree=None,
                                            source="main")
        self.assertEqual(self.cli("restore")[0], 2)


# ---------------------------------------------------------------------------
# Collaboration
# ---------------------------------------------------------------------------

class RemoteCommandTests(CliTestCase):
    def test_remote_list(self):
        self.assertEqual(self.ok("remote"), "origin\n")
        self.assertEqual(self.ok("remote", "-v"),
                         "origin\thttp://h:1/p (fetch)\norigin\thttp://h:1/p (push)\n")

    def test_remote_add_remove(self):
        self.ok("remote", "add", "origin", "http://h:1/p")
        self.remote.remote_add.assert_called_with(self.repo, "origin", "http://h:1/p")
        self.ok("remote", "remove", "origin")
        self.remote.remote_remove.assert_called_with(self.repo, "origin")
        self.ok("remote", "rm", "origin")
        self.assertEqual(self.ok("remote", "get-url", "origin"), "http://h:1/p\n")

    def test_remote_usage(self):
        self.assertEqual(self.cli("remote", "add", "origin")[0], 2)
        self.assertEqual(self.cli("remote", "remove")[0], 2)
        self.assertEqual(self.cli("remote", "bogus")[0], 2)

    def test_clone(self):
        rc, out, err = self.cli("clone", "http://h:1/p", "dir")
        self.assertEqual(rc, 0)
        self.remote.clone.assert_called_with("http://h:1/p", "dir", self.cwd)
        self.assertEqual(out, "Cloning into 'p'...\n")
        self.assertEqual(err, "warning: You appear to have cloned an empty repository.\n")
        self.ok("clone", "http://h:1/p")
        self.remote.clone.assert_called_with("http://h:1/p", None, self.cwd)

    def test_clone_error(self):
        self.remote.clone.side_effect = self.E.RemoteError("could not connect to server at http://h:1/p: refused")
        rc, _, err = self.cli("clone", "http://h:1/p")
        self.assertEqual(rc, 1)
        self.assertIn("error: could not connect", err)

    def test_fetch(self):
        out = self.ok("fetch")
        self.remote.fetch.assert_called_with(self.repo, "origin")
        self.remote.remote_url.assert_called_with(self.repo, "origin")
        self.remote.format_fetch.assert_called_with("http://h:1/p", ["change"])
        self.assertIn("main -> origin/main", out)
        self.ok("fetch", "upstream")
        self.remote.fetch.assert_called_with(self.repo, "upstream")

    def test_pull_merged(self):
        out = self.ok("pull")
        self.remote.pull.assert_called_with(self.repo, None, None)
        self.remote.remote_url.assert_called_with(self.repo, "upstream")  # from get_upstream
        self.assertIn("main -> origin/main", out)
        self.assertIn("Merge made by the 'three-way' strategy.", out)
        self.ok("pull", "origin", "main")
        self.remote.pull.assert_called_with(self.repo, "origin", "main")
        self.remote.remote_url.assert_called_with(self.repo, "origin")

    def test_pull_default_remote_without_upstream(self):
        self.ops.get_upstream.return_value = None
        self.ok("pull")
        self.remote.remote_url.assert_called_with(self.repo, "origin")

    def test_pull_conflict(self):
        self.remote.pull.return_value = (["c"], Outcome("conflict", ["CONFLICT (content): Merge conflict in a"]))
        rc, out, _ = self.cli("pull")
        self.assertEqual(rc, 1)
        self.assertIn("CONFLICT", out)

    def test_pull_no_upstream(self):
        self.remote.pull.side_effect = self.E.MinigitError("no upstream 'origin/x' (push with -u first)")
        rc, _, err = self.cli("pull")
        self.assertEqual(rc, 1)
        self.assertIn("no upstream", err)

    def test_push(self):
        out = self.ok("push")
        self.remote.push.assert_called_with(self.repo, None, None, set_upstream=False, force=False, delete=False)
        self.assertEqual(out, "To http://h:1/p\n   1..2  main -> main\n")
        self.ok("push", "-u", "origin", "main")
        self.remote.push.assert_called_with(self.repo, "origin", "main", set_upstream=True, force=False,
                                            delete=False)
        self.ok("push", "--force", "--delete", "origin", "old")
        self.remote.push.assert_called_with(self.repo, "origin", "old", set_upstream=False, force=True,
                                            delete=True)
        self.ok("push", "-f")
        self.remote.push.assert_called_with(self.repo, None, None, set_upstream=False, force=True, delete=False)

    def test_push_rejected(self):
        err_obj = self.E.PushRejectedError("push rejected", results=[{"ref": "refs/heads/main"}], url="u")
        self.remote.push.side_effect = err_obj
        rc, out, err = self.cli("push")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.remote.format_push_rejection.assert_called_with(err_obj, "main", "main")
        self.assertIn("[rejected]", err)
        self.assertIn("pull first", err)

    def test_push_rejected_refspec_names(self):
        err_obj = self.E.PushRejectedError("push rejected", results=[], url="u")
        self.remote.push.side_effect = err_obj
        self.cli("push", "origin", "dev:main")
        self.remote.format_push_rejection.assert_called_with(err_obj, "dev", "main")
        self.cli("push", "origin", "dev")
        self.remote.format_push_rejection.assert_called_with(err_obj, "dev", "dev")

    def test_push_rejected_outside_push_handler(self):
        err_obj = self.E.PushRejectedError("push rejected", results=[{"ref": "refs/heads/dev"}], url="u")
        self.remote.pull.side_effect = err_obj
        rc, _, err = self.cli("pull")
        self.assertEqual(rc, 1)
        self.remote.format_push_rejection.assert_called_with(err_obj, "dev", "dev")
        self.assertIn("[rejected]", err)

    def test_push_auth_errors(self):
        self.remote.push.side_effect = self.E.AuthError("authentication failed for 'u': check user/token (minigit login)")
        rc, _, err = self.cli("push")
        self.assertEqual(rc, 1)
        self.assertIn("error: authentication failed", err)
        self.remote.push.side_effect = self.E.PermissionDeniedError("permission denied: no write access")
        rc, _, err = self.cli("push")
        self.assertEqual(rc, 1)
        self.assertIn("error: permission denied", err)

    def test_login(self):
        out = self.ok("login", "http://h:1", "--user", "alice", "--token", "T")
        self.remote.login.assert_called_with("http://h:1", "alice", "T")
        self.assertEqual(out, "stored credentials for 'http://h:1'\n")
        self.assertEqual(self.cli("login", "http://h:1", "--user", "a")[0], 2)


class ServerCommandTests(CliTestCase):
    def test_serve(self):
        def serve(root, host, port, *, ready_file, max_push_bytes, verbose, on_ready, web):
            on_ready(f"http://{host}:{port}")
            return 0
        self.server.serve = Mock(side_effect=serve)
        self._make_root()
        out = self.ok("serve", "--root", "srv", "--port", "0", "--ready-file", "ready.txt", "--quiet")
        self.server.serve.assert_called_once_with(
            os.path.join(self.cwd, "srv"), "127.0.0.1", 0, ready_file=os.path.join(self.cwd, "ready.txt"),
            max_push_bytes=256 << 20, verbose=False, on_ready=unittest.mock.ANY, web=True)
        self.assertEqual(out, "Serving minigit on http://127.0.0.1:0\n")

    def _make_root(self):
        root = os.path.join(self.cwd, "srv")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "server.json"), "w") as fh:
            fh.write("{}")

    def test_serve_defaults(self):
        self._make_root()
        self.ok("serve", "--root", "srv")
        args, kw = self.server.serve.call_args
        self.assertEqual(args[1:], ("127.0.0.1", 8765))
        self.assertIsNone(kw["ready_file"])
        self.assertTrue(kw["verbose"])
        self.assertIs(kw["web"], True)
        self.assertEqual(self.cli("serve")[0], 2)

    def test_serve_no_web(self):
        self._make_root()
        self.ok("serve", "--root", "srv", "--no-web")
        self.assertIs(self.server.serve.call_args.kwargs["web"], False)

    def test_server_init(self):
        root = os.path.join(self.cwd, "srv")
        out = self.ok("server", "init", "srv")
        self.auth.server_init.assert_called_with(root)
        self.assertIn(root, out)

    def test_server_add_user(self):
        self.assertEqual(self.ok("server", "add-user", "srv", "alice"), "TOKEN123\n")
        self.auth.add_user.assert_called_with(os.path.join(self.cwd, "srv"), "alice", reset=False)
        self.ok("server", "add-user", "srv", "alice", "--reset")
        self.auth.add_user.assert_called_with(os.path.join(self.cwd, "srv"), "alice", reset=True)
        self.auth.server_init.assert_called_with(os.path.join(self.cwd, "srv"))

    def test_server_add_user_exists(self):
        self.auth.add_user.side_effect = self.E.MinigitError("user 'alice' already exists (use --reset)")
        rc, _, err = self.cli("server", "add-user", "srv", "alice")
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err)

    def test_server_create_repo(self):
        out = self.ok("server", "create-repo", "srv", "proj")
        self.auth.create_repo.assert_called_with(os.path.join(self.cwd, "srv"), "proj", readers=("*",),
                                                 writers=("*",), public=False, allow_force=False)
        self.assertEqual(out, "/proj\n")
        self.ok("server", "create-repo", "srv", "proj", "--readers", "alice, bob", "--writers", "alice",
                "--public", "--allow-force")
        self.auth.create_repo.assert_called_with(os.path.join(self.cwd, "srv"), "proj",
                                                 readers=("alice", "bob"), writers=("alice",), public=True,
                                                 allow_force=True)

    def test_server_list(self):
        out = self.ok("server", "list", "srv")
        self.auth.list_server.assert_called_with(os.path.join(self.cwd, "srv"))
        self.assertIn("alice", out)

    def test_server_needs_subcommand(self):
        rc, _, err = self.cli("server")
        self.assertEqual(rc, 2)
        self.assertIn("subcommand", err)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

class PlumbingTests(CliTestCase):
    def test_fsck_clean(self):
        self.assertEqual(self.ok("fsck"), "")
        self.pack.fsck.assert_called_with(self.repo)

    def test_fsck_problems(self):
        self.pack.fsck.return_value = ["missing object abc", "corrupt object def"]
        rc, out, _ = self.cli("fsck")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "missing object abc\ncorrupt object def\n")

    def test_cat_file(self):
        self.assertEqual(self.ok("cat-file", "-p", "HEAD"), "blob content")
        self.ops.cat_file.assert_called_with(self.repo, "-p", "HEAD")
        self.ops.cat_file.return_value = b"commit"
        self.assertEqual(self.ok("cat-file", "-t", "HEAD"), "commit\n")
        self.ops.cat_file.return_value = b"12\n"
        self.assertEqual(self.ok("cat-file", "-s", "HEAD"), "12\n")
        self.assertEqual(self.cli("cat-file", "HEAD")[0], 2)
        self.assertEqual(self.cli("cat-file", "-p", "-t", "HEAD")[0], 2)

    def test_ls_files(self):
        self.assertEqual(self.ok("ls-files"), "a.txt\nb.txt\n")
        self.ops.ls_files.assert_called_with(self.repo, stage=False)
        self.ok("ls-files", "-s")
        self.ops.ls_files.assert_called_with(self.repo, stage=True)

    def test_hash_object(self):
        self.assertEqual(self.ok("hash-object", "a.txt"), SHA + "\n")
        self.ops.hash_object_cmd.assert_called_with(self.repo, os.path.join(self.cwd, "a.txt"), False)
        self.ok("hash-object", "-w", "a.txt")
        self.ops.hash_object_cmd.assert_called_with(self.repo, os.path.join(self.cwd, "a.txt"), True)

    def test_hash_object_outside_repo(self):
        self.repo_mod.Repository.discover.side_effect = self.E.NotARepositoryError("no repo")
        self.ok("hash-object", "a.txt")
        self.ops.hash_object_cmd.assert_called_with(None, os.path.join(self.cwd, "a.txt"), False)
        self.assertEqual(self.cli("hash-object", "-w", "a.txt")[0], 1)

    def test_rev_parse(self):
        self.assertEqual(self.ok("rev-parse", "HEAD", "main"), SHA + "\n" + SHA + "\n")
        self.revparse.resolve_object.assert_called_with(self.repo, "main")
        self.assertEqual(self.ok("rev-parse", "--short", "HEAD"), SHA[:7] + "\n")
        self.assertEqual(self.cli("rev-parse")[0], 2)

    def test_rev_parse_unknown(self):
        self.revparse.resolve_object.side_effect = self.E.RevisionError("unknown revision 'zzz'")
        rc, _, err = self.cli("rev-parse", "zzz")
        self.assertEqual(rc, 1)
        self.assertEqual(err, "error: unknown revision 'zzz'\n")

    def test_help_command(self):
        self.assertIn("usage: minigit", self.ok("help"))
        self.assertIn("usage: minigit commit", self.ok("help", "commit"))
        rc, _, err = self.cli("help", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("no help available", err)


class FallbackTests(unittest.TestCase):
    """The CLI keeps working while minigit.errors has no classes yet."""

    def test_usage_error_without_errors_module(self):
        with patch.dict(sys.modules, {"minigit.errors": types.ModuleType("minigit.errors")}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cli.main(["frobnicate"])
            self.assertEqual(rc, 1)  # fallback exit code: the real UsageError gives 2
            self.assertIn("error: argument <command>: invalid choice", err.getvalue())


if __name__ == "__main__":
    unittest.main()

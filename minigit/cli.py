"""minigit.cli -- owner: M7 cli. See SPEC.md section 8 for the public API.

The command line front end. This is the only module that prints. Every
library module is imported lazily (``_m("ops")``) so that ``--help`` and
usage errors work even while other modules are still being built, and so
tests can substitute fakes through ``sys.modules``.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Sequence


# ---------------------------------------------------------------------------
# Lazy access to the library
# ---------------------------------------------------------------------------

def _m(name: str):
    """Import ``minigit.<name>`` at call time (honours sys.modules patches)."""
    return importlib.import_module("minigit." + name)


class _Fallback(Exception):
    """Stand-in for MinigitError while minigit.errors is not available."""

    exit_code = 1

    def __init__(self, message: str = "", *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def _E(name: str):
    """Return an error class from minigit.errors (or a fallback)."""
    return getattr(_m("errors"), name, _Fallback)


def _usage(message: str, hint: str | None = None):
    return _E("UsageError")(message, hint=hint)


# ---------------------------------------------------------------------------
# Output helpers (data to stdout as UTF-8 bytes, errors to stderr)
# ---------------------------------------------------------------------------

def _write(stream, data: bytes) -> None:
    buf = getattr(stream, "buffer", None)
    try:
        stream.flush()
    except Exception:
        pass
    try:
        if buf is not None:
            buf.write(data)
            buf.flush()
        else:
            stream.write(data.decode("utf-8", "replace"))
            stream.flush()
    except OSError as e:
        # Windows reports a pipe whose reader has gone ('minigit log | head') as EINVAL
        if isinstance(e, BrokenPipeError) or e.errno in (22, 32):
            raise BrokenPipeError(32, "Broken pipe") from None
        raise


def _out(data: bytes | str) -> None:
    """Write raw data to stdout (bytes are written verbatim)."""
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogateescape")
    if data:
        _write(sys.stdout, data)


def _say(text: str | None) -> None:
    """Write a message to stdout with exactly one trailing newline."""
    if not text:
        return
    if not text.endswith("\n"):
        text += "\n"
    _out(text)


def _stderr(text: str) -> None:
    if text and not text.endswith("\n"):
        text += "\n"
    _write(sys.stderr, text.encode("utf-8", "surrogateescape"))


def _warn(text: str) -> None:
    _stderr(text if text.startswith("warning:") else "warning: " + text)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose errors raise UsageError (exit 129, as git) instead of exiting."""

    def error(self, message: str) -> None:  # type: ignore[override]
        _stderr(self.format_usage().rstrip())
        hint = None
        if "expected one argument" in message:
            hint = "a value that starts with '-' must be attached to its option, e.g. --token=-abc"
        elif message.startswith("unrecognized arguments: "):
            hint = self._option_hint(message[len("unrecognized arguments: "):].split())
        raise _usage(message, hint)

    def _option_hint(self, extras: list[str]) -> str | None:
        """'did you mean --oneline?' for a mistyped option (git only prints the usage)."""
        import difflib

        known = [o for o in self._option_string_actions if o.startswith("--")]
        for arg in extras:
            if arg.startswith("--"):
                close = difflib.get_close_matches(arg.split("=", 1)[0], known, n=1, cutoff=0.6)
                if close:
                    return f"did you mean '{close[0]}'? (see '{self.prog} -h')"
        return None

    def _print_message(self, message: str, file=None) -> None:  # type: ignore[override]
        # --help/--version text goes out as UTF-8 bytes with "\n" newlines like all other
        # output (a text-mode stream would turn them into "\r\n" on Windows).
        if message:
            _write(file if file is not None else sys.stderr, message.encode("utf-8", "replace"))


class _IntermixedParser(_Parser):
    """Parser that lets options appear between optional positionals (git's 'push origin --delete b')."""
    _inner = False

    def parse_known_args(self, args=None, namespace=None):  # type: ignore[override]
        if self._inner:
            return super().parse_known_args(args, namespace)
        self._inner = True
        try:
            return self.parse_known_intermixed_args(args, namespace)
        finally:
            self._inner = False


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port {value!r}: expected a number from 0 to 65535") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {value}: must be between 0 and 65535")
    return port


def _version_string() -> str:
    try:
        return "minigit " + str(importlib.import_module("minigit").__version__)
    except Exception:  # pragma: no cover - the package always has a version
        return "minigit 0.3.0"


def _add_blame_parser(sub) -> None:
    """Parser for ``blame`` (handler: minigit/blame.py, imported lazily)."""
    text = "Show which commit and author last modified each line of a file"
    s = sub.add_parser("blame", help=text, description=text)
    s.add_argument("-L", dest="line_range", default=None, metavar="START[,END|,+N]",
                   help="only blame these lines (1-based, inclusive)")
    s.add_argument("-s", dest="suppress", action="store_true", help="suppress author name and date")
    s.add_argument("-e", "--show-email", dest="show_email", action="store_true",
                   help="show the author email instead of the name")
    s.add_argument("--porcelain", action="store_true", help="machine readable output")
    s.add_argument("args", nargs="*", metavar="[rev] path", help="optional revision, then the file")


def _add_grep_parser(sub) -> None:
    """Parser for ``grep`` (handler: minigit/grep.py, imported lazily)."""
    text = ("Print lines of tracked files (or a tree) that match a pattern. Patterns are Python "
            "regular expressions (-i folds ASCII only). From a subdirectory only its files are "
            "searched and paths are printed relative to it, as git does.")
    s = sub.add_parser("grep", help="Print lines of tracked files (or a tree) that match a pattern",
                       description=text)
    s.add_argument("-i", "--ignore-case", dest="ignore_case", action="store_true")
    s.add_argument("-n", "--line-number", dest="line_numbers", action="store_true")
    g = s.add_mutually_exclusive_group()
    g.add_argument("-l", "--files-with-matches", dest="names_only", action="store_true")
    g.add_argument("-c", "--count", action="store_true")
    s.add_argument("-w", "--word-regexp", dest="word", action="store_true")
    s.add_argument("-F", "--fixed-strings", dest="fixed", action="store_true")
    s.add_argument("-e", dest="patterns", action="append", default=[], metavar="PATTERN",
                   help="pattern (repeatable; use -e=-x for a pattern starting with '-')")
    s.add_argument("--cached", action="store_true", help="search the index instead of the working tree")
    s.add_argument("args", nargs="*", metavar="[pattern] [rev] [path...]")


class _Skip:
    """Stands in for the parsers of the commands ``build_parser(only=...)`` leaves out.

    Every method call (add_argument, add_mutually_exclusive_group, add_subparsers, ...) is a
    no-op that returns the same object, and attribute assignment is ignored.
    """

    def __getattr__(self, name):
        return self._noop

    def __setattr__(self, name, value) -> None:
        pass

    def _noop(self, *args, **kwargs):
        return self


_SKIP = _Skip()

# Commands whose parsers live in their own module (``<module>.add_parser(sub)``).
_MODULE_PARSERS = {
    "cherry-pick": "cherry", "revert": "cherry", "stash": "stash", "pr": "pr",
    "rebase": "rebase", "tag": "tags", "gc": "gc",
}


class _OnlySub:
    """Subparsers proxy for ``build_parser(only=...)``: only the selected command is really added."""

    def __init__(self, sub, only: str) -> None:
        self._sub = sub
        self._only = only

    def add_parser(self, name, **kw):
        if name != self._only:
            return _SKIP
        return self._sub.add_parser(name, **kw)


def build_parser(only: str | None = None) -> argparse.ArgumentParser:
    """Build the argparse parser. prog="minigit"; errors raise UsageError.

    With ``only``, just that command's subparser is built (and only its module imported): the
    other ~40 cost most of the CLI's startup time. ``only=""`` builds the top-level parser alone.
    """
    p = _Parser(
        prog="minigit",
        description="minigit: a small, working git clone with a shared HTTP server.",
        epilog="Run 'minigit <command> --help' for help on a command.",
    )
    p.add_argument("-C", dest="chdir", action="append", default=[], metavar="DIR",
                   help="run as if minigit was started in DIR")
    p.add_argument("--version", action="version", version=_version_string())
    real_sub = p.add_subparsers(dest="command", metavar="<command>", parser_class=_Parser)
    p._minigit_sub = real_sub  # type: ignore[attr-defined]
    sub = real_sub if only is None else _OnlySub(real_sub, only)

    def cmd(name, help_, **kw):
        return sub.add_parser(name, help=help_, description=help_, **kw)

    def module_parsers(module: str) -> None:
        if only is None or _MODULE_PARSERS.get(only) == module:
            _m(module).add_parser(sub)

    # --- repository ------------------------------------------------------
    s = cmd("init", "Create an empty repository")
    s.add_argument("--bare", action="store_true", help="create a bare repository")
    s.add_argument("-b", "--initial-branch", default="main", metavar="BRANCH",
                   help="name of the initial branch (default: main)")
    s.add_argument("directory", nargs="?", help="where to create the repository (default: .)")

    s = cmd("config", "Get and set repository or global options")
    s.add_argument("--global", dest="global_", action="store_true", help="use the global config file")
    s.add_argument("-l", "--list", dest="list_", action="store_true", help="list all variables")
    s.add_argument("--unset", action="store_true", help="remove a variable")
    s.add_argument("key", nargs="?", help="key such as user.name")
    s.add_argument("value", nargs="?", help="value to set")

    # --- working tree ----------------------------------------------------
    s = cmd("add", "Add file contents to the index")
    s.add_argument("-f", "--force", action="store_true", help="allow adding ignored files")
    s.add_argument("-A", "--all", dest="all_", action="store_true",
                   help="stage every change in the whole work tree (new, modified and deleted files), "
                        "from the repository root whatever the current directory")
    s.add_argument("-u", "--update", action="store_true",
                   help="stage modified and deleted tracked files only, never new ones "
                        "(the whole tree, or the tracked files under the given pathspecs)")
    s.add_argument("pathspecs", nargs="*", metavar="pathspec", help="files, directories or '.'")

    s = cmd("rm", "Remove files from the working tree and the index")
    s.add_argument("--cached", action="store_true", help="only remove from the index")
    s.add_argument("-r", dest="recursive", action="store_true", help="allow recursive removal")
    s.add_argument("-f", "--force", action="store_true", help="override the up-to-date check")
    s.add_argument("paths", nargs="*", metavar="path")

    s = cmd("status", "Show the working tree status")
    s.add_argument("-s", "--short", dest="short", action="store_true",
                   help="short 'XY path' lines, relative to the current directory")
    s.add_argument("--porcelain", dest="porcelain", action="store_true",
                   help="machine readable 'XY path' lines (repo-root paths, one per file)")

    s = cmd("diff", "Show changes between the working tree, the index and commits")
    s.add_argument("--staged", "--cached", dest="staged", action="store_true",
                   help="compare the index with HEAD, or with <rev> if one is given")
    s.add_argument("--stat", action="store_true",
                   help="print a diffstat (one ' path | N +-' line per file and a summary) instead of the patch")
    s.add_argument("args", nargs="*", metavar="rev|a..b|path",
                   help="zero, one or two revisions, then '--' and paths; "
                        "for unmerged paths, the working file (with conflict markers) "
                        "is diffed against stage 2 (ours)")

    s = cmd("commit", "Record changes to the repository")
    s.add_argument("-m", "--message", dest="messages", action="append", default=[], metavar="MSG",
                   help="commit message (repeat for paragraphs)")
    s.add_argument("-a", "--all", dest="all_", action="store_true",
                   help="stage modified and deleted tracked files first")
    s.add_argument("--allow-empty", action="store_true", help="allow a commit with no changes")
    s.add_argument("--amend", action="store_true",
                   help="replace the HEAD commit: same parents, the index as tree, the original author; "
                        "-m replaces the message, else it is kept (the old commit goes to ORIG_HEAD)")

    s = cmd("log", "Show the commit history")
    s.add_argument("--oneline", action="store_true", help="one line per commit")
    s.add_argument("-n", "--max-count", dest="max_count", type=int, default=None, metavar="N",
                   help="limit the number of commits")
    s.add_argument("--all", dest="all_", action="store_true", help="start from every ref")
    s.add_argument("--graph", action="store_true",
                   help="draw the history as an ASCII graph on the left ('*' commits, '|' lanes)")
    s.add_argument("--stat", action="store_true",
                   help="append each commit's diffstat (against its first parent; nothing for merges)")
    s.add_argument("revs", nargs="*", metavar="rev|path",
                   help="starting points (default: HEAD), then '--' and paths: only commits that change "
                        "one of the paths (a directory matches everything under it); a path that is also "
                        "a revision must be given after '--'")

    s = cmd("show", "Show a commit, blob or tree")
    s.add_argument("--stat", action="store_true", help="print a diffstat instead of the patch")
    s.add_argument("-m", dest="each_parent", action="store_true",
                   help="for a merge commit, show the diff against each parent in turn")
    s.add_argument("spec", nargs="?", default="HEAD",
                   help="revision, or rev:path (default: HEAD); a merge commit is diffed against its "
                        "first parent, after a line that says so")
    _add_blame_parser(sub)
    _add_grep_parser(sub)

    # --- branching -------------------------------------------------------
    s = cmd("branch", "List, create or delete branches, or set a branch's upstream")
    s.add_argument("-v", "--verbose", action="count", default=0,
                   help="show sha and subject; -vv also shows the upstream and ahead/behind counts "
                        "([origin/main: ahead 2, behind 1], [origin/main: gone])")
    s.add_argument("-r", "--remotes", action="store_true", help="list remote-tracking branches")
    s.add_argument("-a", "--all", dest="all_", action="store_true", help="list local and remote branches")
    s.add_argument("-d", "--delete", action="store_true", help="delete a fully merged branch")
    s.add_argument("-D", dest="force_delete", action="store_true", help="delete a branch, even if unmerged")
    s.add_argument("-u", "--set-upstream-to", dest="set_upstream_to", metavar="REMOTE/BRANCH",
                   help="make BRANCH (default: the current branch) track REMOTE/BRANCH")
    s.add_argument("--unset-upstream", dest="unset_upstream", action="store_true",
                   help="forget the upstream of BRANCH (default: the current branch)")
    s.add_argument("name", nargs="?", help="branch to create or delete, or whose upstream to change")
    s.add_argument("start", nargs="?", help="start point for a new branch (default: HEAD)")

    s = cmd("switch", "Switch branches")
    s.add_argument("-c", "--create", metavar="BRANCH", help="create BRANCH and switch to it")
    s.add_argument("--detach", action="store_true", help="detach HEAD at the given revision")
    s.add_argument("args", nargs="*", metavar="branch|rev [start]")

    s = cmd("checkout", "Switch branches, detach HEAD or restore files")
    s.add_argument("-b", dest="branch", metavar="BRANCH", help="create BRANCH and switch to it")
    s.add_argument("args", nargs="*", metavar="branch|rev", help="target, or 'rev -- paths'")

    s = cmd("merge", "Join another history into the current branch")
    s.add_argument("--abort", action="store_true", help="abort the current conflicted merge")
    s.add_argument("-m", "--message", default=None, metavar="MSG", help="merge commit message")
    s.add_argument("--no-ff", dest="no_ff", action="store_true", help="always create a merge commit")
    s.add_argument("--allow-unrelated-histories", dest="allow_unrelated", action="store_true",
                   help="merge histories that share no commit (e.g. two root commits)")
    s.add_argument("rev", nargs="?", help="branch or revision to merge")
    module_parsers("cherry")  # cherry-pick, revert (minigit/cherry.py)
    module_parsers("stash")  # stash (minigit/stash.py)
    module_parsers("pr")  # pr (minigit/pr.py)
    module_parsers("rebase")  # rebase (minigit/rebase.py)
    module_parsers("tags")  # tag (minigit/tags.py)
    module_parsers("gc")  # gc (minigit/gc.py)

    s = cmd("reset", "Reset HEAD, the index or files")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--soft", action="store_true", help="move HEAD only")
    g.add_argument("--mixed", action="store_true", help="move HEAD and reset the index (default)")
    g.add_argument("--hard", action="store_true", help="move HEAD, reset the index and the working tree")
    s.add_argument("args", nargs="*", metavar="rev|path", help="revision, or 'rev -- paths'")

    s = cmd("restore", "Restore working tree or index files")
    s.add_argument("--staged", action="store_true", help="restore the index")
    s.add_argument("--worktree", action="store_true", help="restore the working tree")
    s.add_argument("-s", "--source", default=None, metavar="REV", help="restore from REV")
    s.add_argument("paths", nargs="*", metavar="path")

    # --- collaboration ---------------------------------------------------
    s = cmd("remote", "Manage remotes: 'remote add NAME URL', 'remote remove NAME', 'remote -v'")
    s.add_argument("-v", "--verbose", action="store_true", help="show URLs")
    s.add_argument("args", nargs="*", metavar="add NAME URL | remove NAME")

    s = cmd("clone", "Clone a repository from a minigit server")
    s.add_argument("url", help="http://[user:token@]host:port/repo")
    s.add_argument("directory", nargs="?", help="target directory")

    s = cmd("fetch", "Download objects and update remote-tracking branches")
    s.add_argument("--prune", action="store_true",
                   help="delete remote-tracking branches that no longer exist on the server "
                        "(fetch always does this; the option is accepted for git compatibility)")
    s.add_argument("--tags", dest="all_tags", action="store_true",
                   help="fetch every tag, not only the tags of commits being fetched")
    s.add_argument("remote", nargs="?", help="remote name (default: origin)")

    s = cmd("pull", "Fetch and merge (or, with --rebase, rebase onto) the upstream branch")
    s.add_argument("--rebase", dest="rebase", action="store_true",
                   help="rebase the current branch onto the fetched branch instead of merging "
                        "(a conflict stops like 'minigit rebase' does: --continue, --skip or --abort it)")
    s.add_argument("--no-rebase", dest="rebase", action="store_false", default=False,
                   help="merge (the default)")
    s.add_argument("--allow-unrelated-histories", dest="allow_unrelated", action="store_true",
                   help="merge even if the histories share no commit")
    s.__class__ = _IntermixedParser  # options may come before, between or after the positionals
    s.add_argument("remote", nargs="?", help="remote name")
    s.add_argument("branch", nargs="?", help="remote branch")

    s = cmd("push", "Upload the current branch (or a tag) to a remote")
    s.add_argument("-u", "--set-upstream", dest="set_upstream", action="store_true",
                   help="remember the upstream for pull")
    s.add_argument("-f", "--force", action="store_true",
                   help="allow a non-fast-forward update (or replace a tag on the remote)")
    s.add_argument("--delete", action="store_true", help="delete the remote branch or tag")
    s.add_argument("--tags", dest="all_tags", action="store_true", help="push every local tag")
    s.__class__ = _IntermixedParser  # options may come before, between or after the positionals
    s.add_argument("remote", nargs="?", help="remote name")
    s.add_argument("refspec", nargs="?", metavar="SRC[:DST]", help="branch or tag to push")

    s = cmd("login", "Store credentials for a server")
    s.add_argument("url", help="http://host:port[/repo]")
    s.add_argument("--user", required=True, help="user name")
    s.add_argument("--token", required=True, help="access token")

    s = cmd("serve", "Run the minigit HTTP server",
            epilog="Browse the repositories at http://HOST:PORT/-/ (unless --no-web).")
    s.add_argument("--root", required=True, help="server root directory")
    s.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default: 127.0.0.1; 0.0.0.0 accepts other machines)")
    s.add_argument("--init", action="store_true",
                   help="create the server root if it does not exist yet")
    s.add_argument("--port", type=_port, default=8765, help="port (0 picks a free port)")
    s.add_argument("--ready-file", dest="ready_file", default=None, metavar="FILE",
                   help="write HOST:PORT to FILE once listening")
    s.add_argument("--quiet", action="store_true", help="do not log requests")
    s.add_argument("--no-web", dest="no_web", action="store_true",
                   help="do not serve the read-only web UI at /-/")
    s.add_argument("--max-push-bytes", dest="max_push_bytes", type=int, default=256 << 20, metavar="N",
                   help="reject pushes larger than N bytes (default: 256 MiB)")

    s = cmd("server", "Administer a server root: users, tokens, repositories, permissions")
    if s is not _SKIP:
        ss = s.add_subparsers(dest="server_cmd", metavar="<subcommand>", parser_class=_Parser)
        _m("serveradmin").add_server_parsers(ss)

    # --- plumbing --------------------------------------------------------
    cmd("fsck", "Verify the object store and ref connectivity")

    s = cmd("cat-file", "Print an object's content, type or size")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("-p", dest="pretty", action="store_true", help="pretty print the content")
    g.add_argument("-t", dest="type_", action="store_true", help="print the type")
    g.add_argument("-s", dest="size", action="store_true", help="print the size")
    s.add_argument("spec", help="sha or revision")

    s = cmd("ls-files", "List files in the index")
    s.add_argument("-s", "--stage", action="store_true", help="show mode, sha and stage")

    s = cmd("hash-object", "Compute a blob's object id")
    s.add_argument("-w", dest="write", action="store_true", help="write the blob to the store")
    s.add_argument("path")

    s = cmd("rev-parse", "Resolve revisions to object ids")
    s.add_argument("--short", action="store_true", help="print abbreviated ids")
    s.add_argument("revs", nargs="+", metavar="rev")

    s = cmd("help", "Show help for a command")
    s.add_argument("topic", nargs="?")
    return p


# ---------------------------------------------------------------------------
# Small helpers shared by the handlers
# ---------------------------------------------------------------------------

def _repo(cwd: str):
    return _m("repo").Repository.discover(cwd)


def _abs(cwd: str, path: str | None) -> str:
    return os.path.abspath(os.path.join(cwd, path or "."))


def _is_rev(repo, spec: str) -> bool:
    revparse = _m("revparse")
    base = _E("MinigitError")
    try:
        revparse.resolve_rev(repo, spec)
        return True
    except (base, _Fallback):
        return False


def _is_branch_or_dwim(repo, name: str) -> bool:
    if _m("revparse").resolve_local_branch(repo, name):
        return True
    remotes = repo.refs.list("refs/remotes/")
    matches = [r for r in remotes if r.split("/", 3)[-1] == name]
    return len(matches) == 1


def _path_exists(cwd: str, path: str) -> bool:
    return os.path.lexists(os.path.join(cwd, path))


def _split_revs_paths(repo, cwd: str, args: list[str]) -> tuple[list[str], list[str]]:
    """Without '--': leading revisions, then paths (first existing non-rev wins)."""
    revs: list[str] = []
    paths: list[str] = []
    for a in args:
        if paths:
            paths.append(a)
            continue
        if ".." in a and not _path_exists(cwd, a):
            left, _, right = a.partition("..")
            if _is_rev(repo, left or "HEAD") and _is_rev(repo, right or "HEAD"):
                revs.append(a)
                continue
        if _is_rev(repo, a):
            revs.append(a)
        elif _path_exists(cwd, a):
            paths.append(a)
        else:
            revs.append(a)  # let the library produce "unknown revision"
    return revs, paths


def _restore_paths(repo, cwd: str, rev: str | None, paths: list[str]) -> None:
    ops = _m("ops")
    if rev is None:
        ops.restore(repo, paths, cwd)
    else:
        ops.restore(repo, paths, cwd, staged=True, worktree=True, source=rev)


def _default_remote(repo) -> str:
    try:
        branch = repo.refs.current_branch()
        if branch:
            up = _m("ops").get_upstream(repo, branch)
            if up:
                return up[0]
    except Exception:
        pass
    return "origin"


def _push_names(repo, refspec: str | None) -> tuple[str, str]:
    if refspec:
        src, _, dst = refspec.partition(":")
        return src, (dst or src)
    try:
        branch = repo.refs.current_branch() or "HEAD"
    except Exception:
        branch = "HEAD"
    return branch, branch


# ---------------------------------------------------------------------------
# Command handlers: cmd_<name>(ns, cwd) -> int | None
# ---------------------------------------------------------------------------

def cmd_init(ns, cwd):
    msg = _m("ops").init(_abs(cwd, ns.directory), bare=ns.bare, initial_branch=ns.initial_branch)
    _say(msg)


def cmd_config(ns, cwd):
    if not ns.list_ and not ns.key:
        raise _usage("config: a key is required (or --list)")
    if ns.unset and ns.value is not None:
        raise _usage("config: --unset takes only a key")
    try:
        repo = _repo(cwd)
    except _E("NotARepositoryError"):
        reading = ns.key is not None and ns.value is None and not ns.unset
        if ns.global_ or ns.list_ or reading:
            repo = None
        else:
            raise
    result = _m("ops").config_cmd(repo, ns.key, ns.value, global_=ns.global_,
                                  list_=ns.list_, unset=ns.unset)
    if result is not None:
        _say(result)


def cmd_add(ns, cwd):
    specs = list(ns.pathspecs) + list(ns.after)
    if ns.all_ and ns.update:
        raise _usage("add: -A and -u cannot be used together")
    if not specs and not (ns.all_ or ns.update):
        # as git: a note, not an error (exit 0), so 'minigit add $files' with no files is harmless
        _stderr("Nothing specified, nothing added.\n"
                "hint: use 'minigit add <pathspec>...', 'minigit add .' or 'minigit add -A'")
        return 0
    extra = {}
    if ns.all_:
        extra["all_"] = True
    if ns.update:
        extra["update"] = True
    _m("ops").add(_repo(cwd), specs, cwd, force=ns.force, **extra)


def cmd_rm(ns, cwd):
    paths = list(ns.paths) + list(ns.after)
    if not paths:
        raise _E("FatalError")("No pathspec was given. Which files should I remove?")
    lines = _m("ops").rm(_repo(cwd), paths, cwd, cached=ns.cached, recursive=ns.recursive,
                         force=ns.force)
    _say("\n".join(lines))


def cmd_status(ns, cwd):
    status = _m("status")
    repo = _repo(cwd)
    st = status.compute_status(repo)
    if ns.porcelain:
        _say(status.format_status_porcelain(st))
        return
    view = getattr(_m("ops"), "status_view", None)
    if view is not None:
        st = view(repo, st, cwd)  # cwd-relative paths, collapsed untracked dirs (as git shows them)
    if ns.short:
        _say(status.format_status_porcelain(st))
        return
    cfg = getattr(repo, "config", None)   # advice.statusHints=false drops the "(use ...)" lines, as in git
    _say(status.format_status_long(st, hints=cfg.get_bool("advice.statusHints", True) if cfg is not None else True))


def cmd_diff(ns, cwd):
    repo = _repo(cwd)
    if ns.dashdash:
        revs, paths = list(ns.args), list(ns.after)
    else:
        revs, paths = _split_revs_paths(repo, cwd, list(ns.args))
    if len(revs) > 2:
        raise _usage("diff: at most two revisions (or 'a..b') are allowed")
    if ns.staged and len(revs) > 1:
        raise _usage("diff: --staged takes at most one revision")
    extra = {"stat": True} if ns.stat else {}
    _out(_m("diff").diff_command(repo, cwd, staged=ns.staged, revs=revs, pathspecs=paths, **extra))


def cmd_commit(ns, cwd):
    extra = {"amend": True} if ns.amend else {}
    ops = _m("ops")
    repo = _repo(cwd)
    try:
        _sha, summary = ops.commit(repo, list(ns.messages), all_=ns.all_,
                                   allow_empty=ns.allow_empty, stat=True, **extra)
    except _E("NothingToCommit") as e:
        # as git: the status of the work tree, ending with why nothing was committed, on stdout
        report = getattr(ops, "nothing_to_commit_report", None)
        _say(report(repo, e.message, cwd) if report else e.message)
        return 1
    _say(summary)


def _path_in_head(repo, cwd: str, path: str) -> bool:
    """Does ``path`` (relative to ``cwd``) name a file or directory in the HEAD tree?"""
    try:
        rel = repo.relpath(path, cwd) if repo.worktree is not None else path.replace("\\", "/").strip("/")
        flat = repo.head_flat()
    except _E("MinigitError"):
        return False
    return rel == "" or rel in flat or any(p.startswith(rel + "/") for p in flat)


def _split_log_args(repo, cwd: str, args: list[str]) -> tuple[list[str], list[str]]:
    """git's rule for ``log`` arguments without '--': a name that is not a revision but exists in
    the work tree or in HEAD is a path (and so is everything after it); a name that is both a
    revision and a path must be disambiguated with '--'."""
    revs: list[str] = []
    paths: list[str] = []
    for a in args:
        if paths:
            paths.append(a)
            continue
        is_rev = _is_rev(repo, a)
        is_path = _path_exists(cwd, a) or _path_in_head(repo, cwd, a)
        if is_rev and is_path:
            raise _usage(f"log: ambiguous argument '{a}': both revision and filename",
                         hint="use '--' to separate paths from revisions: minigit log [rev...] -- path...")
        if is_path:
            paths.append(a)
        else:
            revs.append(a)  # unknown names stay revisions so the library reports "unknown revision"
    return revs, paths


def _rel_paths(repo, cwd: str, paths: list[str]) -> list[str]:
    if repo.worktree is None:
        return [p.replace("\\", "/").strip("/") for p in paths]
    return [repo.relpath(p, cwd) for p in paths]


def cmd_log(ns, cwd):
    history = _m("history")
    repo = _repo(cwd)
    if ns.max_count is not None and ns.max_count < 0:
        raise _usage("log: -n must not be negative")
    if ns.dashdash:
        revs, paths = list(ns.revs), list(ns.after)
    else:
        revs, paths = _split_log_args(repo, cwd, list(ns.revs))
    rel = _rel_paths(repo, cwd, paths)
    extra = {"paths": rel} if rel else {}
    if ns.graph:
        extra["topo"] = True   # as git, --graph implies --topo-order
    entries = history.log_entries(repo, revs or None, ns.max_count, all_refs=ns.all_, **extra)
    fmt = {}
    if ns.graph:
        fmt["graph"] = True
        if rel:
            fmt["paths"] = rel
    if ns.stat:
        fmt["stat"] = True
    _say(history.format_log(repo, entries, ns.oneline, **fmt))


def cmd_show(ns, cwd):
    extra = {}
    if ns.stat:
        extra["stat"] = True
    if ns.each_parent:
        extra["each_parent"] = True
    _out(_m("history").show(_repo(cwd), ns.spec or "HEAD", **extra))


def cmd_branch(ns, cwd):
    ops = _m("ops")
    repo = _repo(cwd)
    if ns.set_upstream_to is not None or ns.unset_upstream:
        if ns.set_upstream_to is not None and ns.unset_upstream:
            raise _usage("branch: -u/--set-upstream-to and --unset-upstream cannot be combined")
        if ns.delete or ns.force_delete:
            raise _usage("branch: -d/-D cannot be combined with -u/--unset-upstream")
        names = [n for n in (ns.name, ns.start) if n is not None] + list(ns.after)
        if len(names) > 1:
            raise _usage("branch: too many arguments (expected [BRANCH])")
        branch = names[0] if names else None
        if ns.unset_upstream:
            ops.unset_upstream(repo, branch)
        else:
            _say(ops.set_upstream_to(repo, ns.set_upstream_to, branch))
    elif ns.delete or ns.force_delete:
        names = [n for n in (ns.name, ns.start) if n is not None] + list(ns.after)
        if not names:
            raise _usage("branch: a branch name is required with -d/-D")
        for name in names:
            _say(ops.branch_delete(repo, name, force=ns.force_delete))
    elif ns.name or ns.after:
        # 'branch -- NAME [START]': the names after '--' are taken as is
        names = [n for n in (ns.name, ns.start) if n is not None] + list(ns.after)
        if len(names) > 2:
            raise _usage("branch: too many arguments (expected NAME [START])")
        ops.branch_create(repo, names[0], names[1] if len(names) > 1 else None)
    else:
        if ns.dashdash:
            raise _usage("branch: a branch name is required after '--'")
        items = ops.branch_list(repo, remotes=ns.remotes, all_=ns.all_, upstreams=ns.verbose >= 2)
        _say(ops.format_branch_list(items, bool(ns.verbose)))


def _switch(repo, target, *, advice: bool = False, **kw) -> None:
    """``ops.switch`` with git's output: the notes ("Switched to ...", "HEAD is now at ...", a
    detached HEAD left behind) go to stderr, the changes the work tree carries over to stdout.
    ``advice``: ``checkout <rev>`` explains the detached HEAD it creates from a branch."""
    ops = _m("ops")
    detached = getattr(ops, "detached_head", None)
    before = detached(repo) if detached else None
    branch_before = repo.refs.current_branch() if detached else None
    message = ops.switch(repo, target, **kw)
    if detached:
        # 'switch -c B' with no start point leaves the work tree alone, and git lists nothing then
        changes = "" if kw.get("create") and kw.get("start") is None else ops.local_changes(repo)
        if changes:
            _say(changes)
        notes = [ops.leaving_detached_head(repo, before)]
        if advice and before is None and branch_before is not None and target is not None:
            notes.append(ops.detach_advice(repo, target, branch_before))
        for note in notes:
            if note:
                _stderr(note)
    if message:
        _stderr(message)


def cmd_switch(ns, cwd):
    args = list(ns.args)
    if ns.create:
        if len(args) > 1:
            raise _usage("switch -c takes at most one start point")
        _switch(_repo(cwd), None, create=ns.create, start=args[0] if args else None)
    elif ns.detach:
        if len(args) > 1:
            raise _usage("switch --detach takes one revision")
        _switch(_repo(cwd), args[0] if args else "HEAD", detach=True)
    else:
        if not args:
            raise _E("FatalError")("missing branch or commit argument")
        if len(args) != 1:
            raise _usage("switch: expected exactly one branch name")
        _switch(_repo(cwd), args[0])


def cmd_checkout(ns, cwd):
    ops = _m("ops")
    repo = _repo(cwd)
    args, after = list(ns.args), list(ns.after)
    if ns.branch:
        if ns.dashdash or len(args) > 1:
            raise _usage("checkout -b takes at most one start point")
        _switch(repo, None, create=ns.branch, start=args[0] if args else None)
        return
    if ns.dashdash:
        if len(args) > 1:
            raise _usage("checkout: at most one revision before '--'")
        if not after:
            raise _usage("checkout: no paths given after '--'")
        _restore_paths(repo, cwd, args[0] if args else None, after)
        return
    if not args:
        raise _usage("checkout: expected a branch, a revision or '-- <paths>'")
    if len(args) >= 2:
        _restore_paths(repo, cwd, args[0], args[1:])
        return
    target = args[0]
    if target in ("HEAD", "@") and repo.head_commit() is not None:
        return  # as in git: 'checkout HEAD' changes nothing and keeps the branch attached
    if _is_branch_or_dwim(repo, target):
        _switch(repo, target)
    elif _is_rev(repo, target):
        _switch(repo, target, detach=True, advice=True)
    elif _path_exists(cwd, target):
        _restore_paths(repo, cwd, None, [target])
    else:
        try:
            _switch(repo, target)  # raises the library's error (e.g. several remotes have the branch)
        except _E("FatalError") as e:
            if not e.message.startswith("invalid reference"):
                raise
            # git's wording: 'checkout x' that names no branch, revision or file is a pathspec error
            raise _E("MinigitError")(f"pathspec '{target}' did not match any file(s) known to minigit",
                                     hint=f"no branch, tag, commit or file is named '{target}' "
                                          "('minigit branch -a' lists the branches)") from None


def cmd_merge(ns, cwd):
    merge = _m("merge")
    repo = _repo(cwd)
    if ns.abort:
        if ns.rev:
            raise _usage("merge --abort takes no revision")
        merge.merge_abort(repo)
        return 0
    if not ns.rev:
        raise _usage("merge: a revision is required")
    outcome = merge.merge(repo, ns.rev, message=ns.message, no_ff=ns.no_ff, allow_unrelated=ns.allow_unrelated)
    _say("\n".join(outcome.messages))
    return 1 if outcome.kind == "conflict" else 0


def cmd_reset(ns, cwd):
    ops = _m("ops")
    repo = _repo(cwd)
    mode = "hard" if ns.hard else "soft" if ns.soft else "mixed"
    args, after = list(ns.args), list(ns.after)
    if ns.dashdash:
        if len(args) > 1:
            raise _usage("reset: at most one revision before '--'")
        if not after:
            raise _usage("reset: no paths given after '--'")
        rev, paths = (args[0] if args else "HEAD"), after
    elif len(args) >= 2:
        rev, paths = args[0], args[1:]
    elif len(args) == 1 and not _is_rev(repo, args[0]) and _path_exists(cwd, args[0]):
        rev, paths = "HEAD", args
    else:
        _say(ops.reset(repo, args[0] if args else "HEAD", mode))
        return
    if mode != "mixed":
        raise _E("FatalError")(f"Cannot do {mode} reset with paths.",
                               hint=f"'minigit restore --source {rev} --staged --worktree -- <paths>' "
                                    "resets both the index and the files")
    ops.reset_paths(repo, rev, paths, cwd)
    unstaged = getattr(ops, "unstaged_after_reset", None)
    if unstaged:
        _say(unstaged(repo))


def cmd_restore(ns, cwd):
    paths = list(ns.paths) + list(ns.after)
    if not paths:
        raise _E("FatalError")("you must specify path(s) to restore")
    _m("ops").restore(_repo(cwd), paths, cwd, staged=ns.staged,
                      worktree=(True if ns.worktree else None), source=ns.source)


def cmd_remote(ns, cwd):
    remote = _m("remote")
    repo = _repo(cwd)
    args = list(ns.args)
    if not args:
        for name, url in sorted(remote.remote_list(repo).items()):
            _say(f"{name}\t{url} (fetch)\n{name}\t{url} (push)" if ns.verbose else name)
        return
    verb = args[0]
    if verb == "add":
        if len(args) != 3:
            raise _usage("usage: minigit remote add NAME URL")
        remote.remote_add(repo, args[1], args[2])
    elif verb in ("remove", "rm"):
        if len(args) != 2:
            raise _usage("usage: minigit remote remove NAME")
        remote.remote_remove(repo, args[1])
    elif verb == "get-url":
        if len(args) != 2:
            raise _usage("usage: minigit remote get-url NAME")
        _say(remote.remote_url(repo, args[1]))
    else:
        raise _usage(f"remote: unknown subcommand '{verb}' (use add, remove or -v)")


def cmd_clone(ns, cwd):
    _repo_obj, messages = _m("remote").clone(ns.url, ns.directory, cwd)
    for line in messages:
        if line.startswith("warning:"):
            _warn(line)
        else:
            _say(line)


def cmd_fetch(ns, cwd):
    remote = _m("remote")
    repo = _repo(cwd)
    name = ns.remote or _default_remote(repo)
    if ns.all_tags:
        changes = remote.fetch(repo, name, all_tags=True)
    else:
        changes = remote.fetch(repo, name)
    _say(remote.format_fetch(remote.remote_url(repo, name), changes))


def cmd_pull(ns, cwd):
    remote = _m("remote")
    repo = _repo(cwd)
    name = ns.remote or _default_remote(repo)
    try:
        changes, outcome = remote.pull(repo, ns.remote, ns.branch, allow_unrelated=ns.allow_unrelated,
                                       rebase=ns.rebase)
    except _E("MinigitError") as e:
        if getattr(e, "fetch_changes", None):  # the fetch worked, then the merge/rebase refused
            _say(remote.format_fetch(remote.remote_url(repo, name), e.fetch_changes))
        _say("\n".join(getattr(e, "lines", [])))   # what a stopped rebase did before the refusal
        raise
    _say(remote.format_fetch(remote.remote_url(repo, name), changes))
    _say("\n".join(outcome.messages))
    return 1 if outcome.kind == "conflict" else 0


def cmd_push(ns, cwd):
    remote = _m("remote")
    repo = _repo(cwd)
    try:
        if ns.all_tags:
            if ns.refspec or ns.delete or ns.set_upstream:
                raise _usage("push --tags takes only a remote name")
            lines = remote.push_tags(repo, ns.remote, force=ns.force)
        else:
            lines = remote.push(repo, ns.remote, ns.refspec, set_upstream=ns.set_upstream,
                                force=ns.force, delete=ns.delete)
    except _E("PushRejectedError") as e:
        src, dst = _push_names(repo, ns.refspec)
        # push() attaches the names it resolved (e.g. the branch behind HEAD:dst).
        src = getattr(e, "src", None) or src
        dst = getattr(e, "dst", None) or dst
        _stderr(remote.format_push_rejection(e, src, dst))
        return 1
    _say("\n".join(lines))


def cmd_login(ns, cwd):
    _m("remote").login(ns.url, ns.user, ns.token)
    try:
        clean = _m("transport").split_url(ns.url)[0]
    except Exception:
        clean = ns.url
    _say(f"stored credentials for '{clean}'")


def cmd_serve(ns, cwd):
    root = _abs(cwd, ns.root)
    ready = _abs(cwd, ns.ready_file) if ns.ready_file else None
    if os.path.lexists(root) and not os.path.isdir(root):
        raise _E("MinigitError")(f"server root '{root}' is not a directory",
                                 hint="point --root at a directory made with 'minigit server init'")
    if not ns.init and not os.path.isfile(os.path.join(root, "server.json")):
        raise _E("MinigitError")(f"'{root}' is not a minigit server root",
                                 hint=f"create it with 'minigit server init {ns.root}' (or pass --init)")

    def on_ready(url: str) -> None:
        _say(f"Serving minigit on {url}")

    rc = _m("server").serve(root, ns.host, ns.port, ready_file=ready,
                            max_push_bytes=ns.max_push_bytes, verbose=not ns.quiet,
                            on_ready=on_ready, web=not ns.no_web)
    return 0 if rc is None else rc


def _split_users(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ("*",)
    if value.strip() == "":
        return ()  # an explicitly empty list means nobody
    if value.strip() == "*":
        return ("*",)
    return tuple(v.strip() for v in value.split(",") if v.strip())


def cmd_server(ns, cwd):
    return _m("serveradmin").cmd_server(ns, cwd)


def cmd_fsck(ns, cwd):
    problems = _m("pack").fsck(_repo(cwd))
    if problems:
        _say("\n".join(problems))
        if any(not p.startswith("warning: ") for p in problems):
            _stderr("hint: copy a good version of each damaged or missing object from another clone into "
                    ".minigit/objects/; 'minigit gc --prune' refuses to delete anything until fsck is clean")
            return 1
    return 0


def cmd_cat_file(ns, cwd):
    mode = "-p" if ns.pretty else "-t" if ns.type_ else "-s"
    data = _m("ops").cat_file(_repo(cwd), mode, ns.spec)
    if mode != "-p" and data and not data.endswith(b"\n"):
        data += b"\n"
    _out(data)


def cmd_ls_files(ns, cwd):
    _say(_m("ops").ls_files(_repo(cwd), stage=ns.stage))


def cmd_hash_object(ns, cwd):
    try:
        repo = _repo(cwd)
    except _E("NotARepositoryError"):
        if ns.write:
            raise
        repo = None
    _say(_m("ops").hash_object_cmd(repo, os.path.join(cwd, ns.path), ns.write))


def cmd_rev_parse(ns, cwd):
    revparse = _m("revparse")
    repo = _repo(cwd)
    for rev in ns.revs:
        sha = revparse.resolve_object(repo, rev)
        _say(revparse.abbrev(sha) if ns.short else sha)


def cmd_help(ns, cwd):
    parser = build_parser()
    sub = parser._minigit_sub  # type: ignore[attr-defined]
    if not ns.topic:
        parser.print_help()
    elif ns.topic in sub.choices:
        sub.choices[ns.topic].print_help()
    else:
        raise _usage(f"no help available for '{ns.topic}'")


_COMMANDS = {
    "init": cmd_init, "config": cmd_config, "add": cmd_add, "rm": cmd_rm, "status": cmd_status,
    "diff": cmd_diff, "commit": cmd_commit, "log": cmd_log, "show": cmd_show,
    "branch": cmd_branch, "switch": cmd_switch, "checkout": cmd_checkout, "merge": cmd_merge,
    "reset": cmd_reset, "restore": cmd_restore, "remote": cmd_remote, "clone": cmd_clone,
    "fetch": cmd_fetch, "pull": cmd_pull, "push": cmd_push, "login": cmd_login,
    "serve": cmd_serve, "server": cmd_server, "fsck": cmd_fsck, "cat-file": cmd_cat_file,
    "ls-files": cmd_ls_files, "hash-object": cmd_hash_object, "rev-parse": cmd_rev_parse,
    "help": cmd_help,
    "blame": lambda ns, cwd: _m("blame").cmd_blame(ns, cwd),
    "grep": lambda ns, cwd: _m("grep").cmd_grep(ns, cwd),
    "cherry-pick": lambda ns, cwd: _m("cherry").cmd_cherry_pick(ns, cwd),
    "revert": lambda ns, cwd: _m("cherry").cmd_revert(ns, cwd),
    "stash": lambda ns, cwd: _m("stash").cmd_stash(ns, cwd),
    "pr": lambda ns, cwd: _m("pr").cmd_pr(ns, cwd),
    "rebase": lambda ns, cwd: _m("rebase").cmd_rebase(ns, cwd),
    "tag": lambda ns, cwd: _m("tags").cmd_tag(ns, cwd),
    "gc": lambda ns, cwd: _m("gc").cmd_gc(ns, cwd),
}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

# Commands whose handlers read ns.after / ns.dashdash; every other command rejects '--'.
_TAKES_PATHS_AFTER_DASHDASH = frozenset({"add", "rm", "diff", "log", "checkout", "reset", "restore", "branch",
                                        "blame", "grep", "tag"})


def _split_dashdash(argv: list[str]) -> tuple[list[str], list[str], bool]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:], True
    return argv, [], False


def _command_index(argv: list[str]) -> int | None:
    """Index of the subcommand name in ``argv`` (after the global options), or None."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-C":
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return i
    return None


def _expand_log_count(argv: list[str]) -> list[str]:
    """git's ``log -<n>`` shorthand: ``log -3`` is ``log -n 3``."""
    i = _command_index(argv)
    if i is None or argv[i] != "log":
        return argv
    out = argv[: i + 1]
    for a in argv[i + 1:]:
        if len(a) > 1 and a[0] == "-" and a[1:].isdigit():
            out.append("--max-count=" + a[1:])
        else:
            out.append(a)
    return out


def _resolve_cwd(dirs: Sequence[str]) -> str:
    cwd = os.getcwd()
    for d in dirs:
        cwd = os.path.abspath(os.path.join(cwd, d))
        if not os.path.isdir(cwd):
            raise _E("FatalError")(f"cannot change to '{d}': No such file or directory")
    return cwd


def _unknown_command(name: str) -> int:
    """git's "'x' is not a git command" with the closest command names; exit 1 like git."""
    import difflib

    def score(c: str) -> float:
        return difflib.SequenceMatcher(None, name, c).ratio()

    close = difflib.get_close_matches(name, sorted(_COMMANDS), n=3, cutoff=0.6)
    close = [c for c in close if score(c) >= score(close[0]) - 0.05] if close else []  # git: the best only
    text = f"minigit: '{name}' is not a minigit command. See 'minigit --help'."
    if close:
        head = "The most similar command is" if len(close) == 1 else "The most similar commands are"
        text += "\n\n" + head + "".join(f"\n\t{c}" for c in close)
    _stderr(text)
    return 1


def _parser_for(argv: list[str]) -> argparse.ArgumentParser | None:
    """The parser ``argv`` needs: only its command's subparser when that is enough (fast path)."""
    i = _command_index(argv)
    if i is None:
        # no command: --version needs no subcommands; help and usage text list them all
        quick = "--version" in argv and not ({"-h", "--help"} & set(argv))
        return build_parser("" if quick else None)
    name = argv[i]
    if name not in _COMMANDS:
        return None
    if {"-h", "--help"} & set(argv[:i]):
        return build_parser()  # 'minigit -h status' prints the top-level help
    return build_parser(name)


def _run(argv: list[str]) -> int:
    before, after, has_dd = _split_dashdash(argv)
    if not before:
        build_parser().print_help()
        return 1
    before = _expand_log_count(before)
    parser = _parser_for(before)
    if parser is None:
        return _unknown_command(before[_command_index(before)])
    ns, extras = parser.parse_known_args(before)
    if extras:
        # Report unknown options with the failing subcommand's usage, not the top-level one.
        sub = parser._minigit_sub.choices.get(ns.command) if ns.command else None  # type: ignore[attr-defined]
        (sub or parser).error("unrecognized arguments: " + " ".join(extras))
    ns.after = after
    ns.dashdash = has_dd
    if ns.command is None:
        build_parser().print_help()
        return 1
    if has_dd and ns.command not in _TAKES_PATHS_AFTER_DASHDASH:
        # Never silently drop arguments: e.g. 'commit -- a' must not commit everything.
        raise _usage(f"{ns.command}: paths after '--' are not supported")
    cwd = _resolve_cwd(ns.chdir)
    global _CWD
    _CWD = cwd
    rc = _COMMANDS[ns.command](ns, cwd)
    return 0 if rc is None else int(rc)



def _hint_lines(hint: str) -> str:
    return "\n".join("hint: " + line if line else "hint:" for line in hint.split("\n"))


_CWD: str | None = None  # the command's directory, for the hints below


def _revision_hint(message: str) -> str | None:
    """For "unknown revision 'mian'": "did you mean 'main'?" (the closest branch, tag or remote branch)."""
    import re

    m = re.match(r"unknown revision '([^'~^:@]+)'$", message)
    if not m or _CWD is None:
        return None
    name = m.group(1)
    try:
        repo = _repo(_CWD)
        names = [r.split("/", 2)[2] if r.startswith(("refs/heads/", "refs/tags/")) else r[len("refs/remotes/"):]
                 for r in repo.refs.list("refs/")]
    except Exception:  # a hint must never turn one error into another
        return None
    import difflib

    close = difflib.get_close_matches(name, sorted(set(names)), n=1, cutoff=0.75)
    return f"did you mean '{close[0]}'?" if close else None


def _report(e) -> int:
    """Print a MinigitError git-style ('fatal:' for exit 128, else 'error:') and return its exit code."""
    code = getattr(e, "exit_code", 1)
    code = int(code) if code else 1
    message = getattr(e, "message", None) or str(e)
    prefix = getattr(e, "prefix", None)
    if prefix is None:  # an error class without the attribute (tests' fakes)
        prefix = "fatal" if code == 128 else "error"
    _stderr(f"{prefix}: {message}" if prefix else message)
    hint = getattr(e, "hint", None)
    if not hint and type(e).__name__ == "RevisionError":
        hint = _revision_hint(message)
    if hint:
        _stderr(_hint_lines(hint))
    return code


def _os_error_text(e: OSError) -> str:
    """'fatal: ...' plus a hint for an OSError no library function turned into a MinigitError."""
    import errno

    where = f": '{e.filename}'" if e.filename else ""
    reason = e.strerror or str(e)
    hint = None
    if e.errno == errno.ENOSPC:
        reason = "no space left on device"
        hint = ("free some disk space and run the command again; 'minigit gc' removes temporary files "
                "a failed write left in the object store")
    elif e.errno == errno.EROFS:
        reason = "read-only file system"
    elif isinstance(e, PermissionError):
        reason = "permission denied"
        hint = ("check that the file or directory is writable by you"
                + ("; another program (an editor, a virus scanner, a sync client) may also hold it "
                   "open: close it and try again" if os.name == "nt" else ""))
    elif e.errno == errno.EIO:
        reason = "input/output error (the disk or network share may be failing)"
    text = f"fatal: {reason}{where}"
    return text + ("\n" + _hint_lines(hint) if hint else "")


def _silence_stdout() -> None:
    """After a broken pipe: point stdout at devnull so the interpreter's final flush stays quiet."""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(fd, sys.stdout.fileno())
    except Exception:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return git's exit code: 0 ok, 1 error, 128 fatal, 129 usage, 130 ctrl-C."""
    if argv is None:
        argv = sys.argv[1:]
    base = _E("MinigitError")
    rejected = _E("PushRejectedError")
    try:
        return _run(list(argv))
    except SystemExit as e:  # argparse --help / --version
        code = e.code
        return 0 if code is None else (code if isinstance(code, int) else 1)
    except KeyboardInterrupt:
        _stderr("interrupted")
        return 130
    except BrokenPipeError:
        # 'minigit log | head': the reader went away; like git, stop quietly (128 + SIGPIPE)
        _silence_stdout()
        return 141
    except (base, _Fallback) as e:
        if rejected is not _Fallback and isinstance(e, rejected):
            try:
                ref = e.results[0].get("ref", "") if getattr(e, "results", None) else ""
                name = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
                name = name or "HEAD"
                _stderr(_m("remote").format_push_rejection(e, name, name))
                return 1
            except Exception:
                pass
        return _report(e)
    except OSError as e:
        _debug_traceback()
        _stderr(_os_error_text(e))
        return 128
    except MemoryError:
        _stderr("fatal: out of memory")
        return 128
    except Exception as e:  # noqa: BLE001 - last resort
        _debug_traceback()
        _stderr(f"fatal: internal error: {e}\n"
                "hint: this is a bug in minigit; run the command again with MINIGIT_DEBUG=1 "
                "to see where it happened")
        return 128


def _debug_traceback() -> None:
    if os.environ.get("MINIGIT_DEBUG") == "1":
        import traceback

        traceback.print_exc()

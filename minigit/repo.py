"""minigit.repo -- owner: M1 core. See SPEC.md section 8 for the public API.

Repository layout (SPEC section 1), discovery, path mapping between user
paths and POSIX repo-relative paths, and the commit identity rules.
"""

from __future__ import annotations

import os

from .config import Config
from .errors import MinigitError, NotARepositoryError
from .objects import Commit, ObjectStore, Signature
from .refs import RefStore, check_ref_name
from .trees import FlatTree, commit_flat
from .util import atomic_write, now_with_tz, parse_tz, unlink_with_retry

GITDIR_NAME = ".minigit"

# In-progress operation -> its state file (features/cherry-pick-revert.md section 3.1).
OPERATION_STATE_FILES = {"merge": "MERGE_HEAD", "cherry-pick": "CHERRY_PICK_HEAD", "revert": "REVERT_HEAD",
                         "rebase": "REBASE_HEAD"}
# A rebase is in progress while this directory exists, even between picks (features/rebase.md).
REBASE_STATE_DIR = "rebase"
# The refusal every other command gives while a rebase is in progress.
REBASE_IN_PROGRESS = "rebase in progress; run 'minigit rebase --continue', '--skip' or '--abort' first"


def _is_bare_gitdir(path: str) -> bool:
    if not (
        os.path.isfile(os.path.join(path, "HEAD"))
        and os.path.isdir(os.path.join(path, "objects"))
        and os.path.isfile(os.path.join(path, "config"))
    ):
        return False
    cfg = Config(os.path.join(path, "config"), global_path=os.path.join(path, "config"))
    local = dict(cfg.items("local"))
    return local.get("core.bare", "").strip().lower() in ("true", "yes", "on", "1")


class Repository:
    def __init__(self, gitdir: str, worktree: str | None, bare: bool) -> None:
        self.gitdir = os.path.abspath(gitdir)
        self.worktree = os.path.abspath(worktree) if worktree is not None else None
        self.bare = bare
        self.objects = ObjectStore(os.path.join(self.gitdir, "objects"))
        self.refs = RefStore(self.gitdir)

    def __repr__(self) -> str:
        return "Repository(gitdir=%r, worktree=%r, bare=%r)" % (self.gitdir, self.worktree, self.bare)

    # -- construction -------------------------------------------------------
    @classmethod
    def init(cls, path: str, *, bare: bool = False, initial_branch: str = "main") -> tuple["Repository", bool]:
        path = os.path.abspath(path)
        check_ref_name("refs/heads/" + initial_branch)
        gitdir = path if bare else os.path.join(path, GITDIR_NAME)
        if os.path.isfile(os.path.join(gitdir, "HEAD")) and os.path.isdir(os.path.join(gitdir, "objects")):
            return cls(gitdir, None if bare else path, bare), True
        try:
            os.makedirs(os.path.join(gitdir, "objects"), exist_ok=True)
            os.makedirs(os.path.join(gitdir, "refs", "heads"), exist_ok=True)
            os.makedirs(os.path.join(gitdir, "refs", "remotes"), exist_ok=True)
        except OSError as e:
            if os.path.lexists(path) and not os.path.isdir(path):
                raise MinigitError("cannot create repository in '%s': not a directory" % path) from None
            raise MinigitError("cannot create repository in '%s': %s" % (path, e.strerror or e)) from None
        cfg_path = os.path.join(gitdir, "config")
        if not os.path.exists(cfg_path):
            atomic_write(cfg_path, ("[core]\n\tbare = %s\n" % ("true" if bare else "false")).encode("ascii"))
        atomic_write(os.path.join(gitdir, "HEAD"), ("ref: refs/heads/%s\n" % initial_branch).encode("utf-8"))
        return cls(gitdir, None if bare else path, bare), False

    @classmethod
    def open(cls, path: str) -> "Repository":
        path = os.path.abspath(path)
        gitdir = os.path.join(path, GITDIR_NAME)
        if os.path.isdir(gitdir) and os.path.isfile(os.path.join(gitdir, "HEAD")):
            return cls(gitdir, path, False)
        if _is_bare_gitdir(path):
            return cls(path, None, True)
        raise NotARepositoryError("not a minigit repository: %s" % path)

    @classmethod
    def discover(cls, start: str) -> "Repository":
        start = os.path.abspath(start)
        cur = start
        while True:
            gitdir = os.path.join(cur, GITDIR_NAME)
            if os.path.isdir(gitdir) and os.path.isfile(os.path.join(gitdir, "HEAD")):
                return cls(gitdir, cur, False)
            if _is_bare_gitdir(cur):
                return cls(cur, None, True)
            parent = os.path.dirname(cur)
            if parent == cur:
                raise NotARepositoryError("not a minigit repository (or any parent): %s" % start)
            cur = parent

    # -- paths --------------------------------------------------------------
    def gpath(self, *parts: str) -> str:
        return os.path.join(self.gitdir, *parts)

    @property
    def config(self) -> Config:
        return Config(self.gpath("config"))

    def require_worktree(self) -> str:
        if self.worktree is None:
            raise MinigitError("this operation must be run in a work tree")
        return self.worktree

    def relpath(self, user_path: str, cwd: str) -> str:
        """POSIX repo-relative path for ``user_path`` resolved against ``cwd``; ``""`` is the root."""
        root = self.require_worktree()
        target = os.path.abspath(os.path.normpath(os.path.join(os.path.abspath(cwd), user_path)))
        nroot = os.path.normcase(root)
        ntarget = os.path.normcase(target)
        if ntarget == nroot:
            return ""
        prefix = nroot if nroot.endswith(os.sep) else nroot + os.sep
        if not ntarget.startswith(prefix):
            raise MinigitError("'%s' is outside repository" % user_path)
        rel = target[len(prefix) :]
        rel = rel.replace(os.sep, "/")
        if os.altsep:
            rel = rel.replace(os.altsep, "/")
        return rel.strip("/")

    def abspath(self, rel: str) -> str:
        root = self.require_worktree()
        if not rel:
            return root
        return os.path.join(root, *rel.split("/"))

    # -- history ------------------------------------------------------------
    def head_commit(self) -> str | None:
        return self.refs.resolve("HEAD")

    def head_flat(self) -> FlatTree:
        return commit_flat(self.objects, self.head_commit())

    def read_commit(self, sha: str) -> Commit:
        return self.objects.commit(sha)

    def merge_head(self) -> str | None:
        return self.refs.read_special("MERGE_HEAD")

    def cherry_pick_head(self) -> str | None:
        return self.refs.read_special("CHERRY_PICK_HEAD")

    def revert_head(self) -> str | None:
        return self.refs.read_special("REVERT_HEAD")

    def operation_in_progress(self) -> tuple[str, str] | None:
        """("rebase", orig_head) while .minigit/rebase/ exists (with or without REBASE_HEAD), else
        ("merge"|"cherry-pick"|"revert", sha) for the first state file present, else None."""
        if os.path.isdir(self.gpath(REBASE_STATE_DIR)):
            orig = self.read_state(REBASE_STATE_DIR + "/orig-head") or b""
            return "rebase", orig.decode("ascii", "replace").strip()
        for kind, name in OPERATION_STATE_FILES.items():
            sha = self.refs.read_special(name)
            if sha is not None:
                return kind, sha
        return None

    def clear_operation_state(self) -> None:
        """Remove every in-progress state file plus MERGE_MSG (commit / reset)."""
        for name in OPERATION_STATE_FILES.values():
            self.remove_state(name)
        self.remove_state("MERGE_MSG")

    # -- identity -----------------------------------------------------------
    def identity(self) -> Signature:
        name = os.environ.get("MINIGIT_AUTHOR_NAME")
        email = os.environ.get("MINIGIT_AUTHOR_EMAIL")
        if not name or not email:
            cfg = self.config
            name = name or cfg.get("user.name")
            email = email or cfg.get("user.email")
        if not name or not email:
            raise MinigitError(
                "please set your identity: minigit config --global user.name \"You\"; "
                "minigit config --global user.email you@example.com"
            )
        date = os.environ.get("MINIGIT_AUTHOR_DATE")
        if date:
            parts = date.split()
            try:
                if len(parts) == 1:
                    ts, tz = int(parts[0]), 0
                elif len(parts) == 2:
                    ts, tz = int(parts[0]), parse_tz(parts[1])
                else:
                    raise ValueError(date)
            except (ValueError, MinigitError):
                raise MinigitError("invalid MINIGIT_AUTHOR_DATE '%s' (expected '<unix> <+hhmm>')" % date) from None
        else:
            ts, tz = now_with_tz()
        return Signature(name.strip(), email.strip(), ts, tz)

    # -- state files (MERGE_MSG etc.) ---------------------------------------
    def read_state(self, name: str) -> bytes | None:
        try:
            with open(self.gpath(name), "rb") as f:
                return f.read()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None

    def write_state(self, name: str, data: bytes) -> None:
        atomic_write(self.gpath(name), data)

    def remove_state(self, name: str) -> None:
        unlink_with_retry(self.gpath(name))

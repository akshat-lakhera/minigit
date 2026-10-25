"""minigit.auth -- owner: M5 server.

server.json handling, token auth and repo ACLs (SPEC.md section 7.1)::

    {"version": 1,
     "users": {"alice": {"token_sha256": "<hex>", "token_created": 1700000000,
                         "tokens": {"ci": {"sha256": "<hex>", "created": 1700000000}}}},
     "repos": {"proj": {"readers": ["*"], "writers": ["*"], "public": false, "allow_force": false}}}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Callable, Sequence

from minigit import errors, util
from minigit.repo import Repository

SERVER_JSON = "server.json"
REPOS_DIR = "repos"
AUDIT_LOG = "audit.log"
LOCK_TIMEOUT = 5.0

_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_TOKEN_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
DEFAULT_TOKEN = "default"  # the name under which the user's ``token_sha256`` is listed


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_user_name(name: str) -> None:
    if not isinstance(name, str) or not _USER_RE.match(name):
        raise errors.MinigitError(
            f"invalid user name '{name}'",
            hint="user names use letters, digits, '_', '.', '-' and are at most 64 characters",
        )


def validate_token_name(name: str) -> None:
    if not isinstance(name, str) or not _TOKEN_NAME_RE.match(name):
        raise errors.MinigitError(
            f"invalid token name '{name}'",
            hint="token names use letters, digits, '_', '.', '-' and are at most 64 characters",
        )


def validate_repo_name(name: str) -> None:
    if not isinstance(name, str) or not _REPO_RE.match(name) or ".." in name:
        raise errors.MinigitError(
            f"invalid repository name '{name}'",
            hint="repository names start with a letter or digit, use letters, digits, '.', '_', '-', "
                 "are at most 100 characters and never contain '..'",
        )


def _default_config() -> dict:
    return {"version": 1, "users": {}, "repos": {}}


def config_path(root: str) -> str:
    return os.path.join(root, SERVER_JSON)


def repos_dir(root: str) -> str:
    return os.path.join(root, REPOS_DIR)


def _repo_dir_name(name: str) -> str:
    """Directory name for repo ``name``. Names Windows would alias ('foo.' is stored as 'foo', 'NUL'
    is a device) get an escaped, portable name; '%' never occurs in a valid repo name, so an escaped
    name cannot clash with another repo."""
    from .util import is_windows_reserved_name

    if name.endswith("."):
        name = name[:-1] + "%2E"
    if is_windows_reserved_name(name):
        name = "%%%02X" % ord(name[0]) + name[1:]
    return name


def repo_dir(root: str, name: str) -> str:
    path = os.path.join(root, REPOS_DIR, _repo_dir_name(name))
    if os.name != "nt" and not os.path.isdir(path):
        legacy = os.path.join(root, REPOS_DIR, name)  # created before names were escaped
        if legacy != path and os.path.isdir(legacy):
            return legacy
    return path


def _load(root: str) -> dict:
    path = config_path(root)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return _default_config()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise errors.MinigitError(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise errors.MinigitError(f"{path} must contain a JSON object")
    data.setdefault("version", 1)
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    if not isinstance(data.get("repos"), dict):
        data["repos"] = {}
    return data


def usable_hash(value: object) -> str | None:
    """The lower-case hex digest in ``value``, or None when it is not a well-formed SHA-256 digest."""
    if isinstance(value, str) and _HASH_RE.match(value):
        return value.lower()
    return None


def user_tokens(record: object) -> list[tuple[str, str, int | None]]:
    """The usable tokens of a user record as ``(name, sha256, created)``: the default token
    (``token_sha256``) first, then the named ones by name. Malformed entries are skipped; ``created``
    is None when unknown (records written before named tokens existed have no time)."""
    out: list[tuple[str, str, int | None]] = []
    if not isinstance(record, dict):
        return out

    def stamp(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    digest = usable_hash(record.get("token_sha256"))
    if digest is not None:
        out.append((DEFAULT_TOKEN, digest, stamp(record.get("token_created"))))
    named = record.get("tokens")
    if isinstance(named, dict):
        for name in sorted(k for k in named if isinstance(k, str)):
            entry = named[name]
            digest = usable_hash(entry.get("sha256")) if isinstance(entry, dict) else None
            if digest is not None:
                out.append((name, digest, stamp(entry.get("created"))))
    return out


def _dump(data: dict) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _modify(root: str, fn: Callable[[dict], object]) -> object:
    """Load server.json under its lock, let ``fn`` mutate it, write it back atomically."""
    os.makedirs(root, exist_ok=True)
    path = config_path(root)
    with util.LockFile(path, timeout=LOCK_TIMEOUT):
        data = _load(root)
        result = fn(data)
        util.atomic_write(path, _dump(data))
    return result


class ServerConfig:
    """A snapshot of ROOT/server.json. The server builds a fresh one for every request."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.data = _load(root)
        self.users: dict = self.data["users"]
        self.repos: dict = self.data["repos"]

    # -- users -------------------------------------------------------------
    def authenticate(self, user: str, token: str) -> bool:
        if not isinstance(user, str) or not isinstance(token, str):
            return False
        candidate = hash_token(token).encode("ascii")
        stored = [h for _, h, _ in user_tokens(self.users.get(user))]
        if not stored:
            # compare anyway so that unknown users cost the same as bad tokens
            hmac.compare_digest(candidate, hash_token("").encode("ascii"))
            return False
        ok = False
        for digest in stored:  # no early exit: every token is compared
            if hmac.compare_digest(candidate, digest.encode("ascii")):
                ok = True
        return ok

    # -- repos -------------------------------------------------------------
    def _repo(self, name: str) -> dict | None:
        rec = self.repos.get(name)
        return rec if isinstance(rec, dict) else None

    def repo_exists(self, name: str) -> bool:
        if not isinstance(name, str) or not _REPO_RE.match(name) or ".." in name:
            return False
        if self._repo(name) is None:
            return False
        return os.path.isdir(repo_dir(self.root, name))

    @staticmethod
    def _listed(user: str | None, names: object) -> bool:
        if user is None or not isinstance(names, list):
            return False
        return "*" in names or user in names

    def can_read(self, user: str | None, repo: str) -> bool:
        rec = self._repo(repo)
        if rec is None:
            return False
        if rec.get("public"):
            return True
        return self._listed(user, rec.get("readers")) or self._listed(user, rec.get("writers"))

    def can_write(self, user: str | None, repo: str) -> bool:
        rec = self._repo(repo)
        if rec is None:
            return False
        return self._listed(user, rec.get("writers"))

    def allow_force(self, repo: str) -> bool:
        rec = self._repo(repo)
        return bool(rec and rec.get("allow_force"))

    def readable_repos(self, user: str | None) -> list[str]:
        return sorted(name for name in self.repos if self.can_read(user, name))


# ---------------------------------------------------------------------------
# admin operations
# ---------------------------------------------------------------------------

def server_init(root: str) -> None:
    """Create ROOT, ROOT/repos and ROOT/server.json if missing. Idempotent."""
    os.makedirs(repos_dir(root), exist_ok=True)
    if not os.path.exists(config_path(root)):
        _modify(root, lambda data: None)


def _new_token() -> str:
    """``secrets.token_urlsafe(24)``, redrawn while it starts with "-": a leading dash
    would make ``minigit login --token <token>`` parse the token as an option."""
    while True:
        token = secrets.token_urlsafe(24)
        if not token.startswith("-"):
            return token


def add_user(root: str, name: str, *, reset: bool = False) -> str:
    """Create a user (or replace its default token with ``reset``) and return the new token.
    Named tokens and any other fields of an existing record are kept."""
    validate_user_name(name)
    server_init(root)
    token = _new_token()

    def mutate(data: dict) -> None:
        if name in data["users"] and not reset:
            raise errors.MinigitError(f"user '{name}' already exists (use --reset to issue a new token)")
        record = data["users"].get(name)
        if not isinstance(record, dict):
            record = data["users"][name] = {}
        record["token_sha256"] = hash_token(token)
        record["token_created"] = int(time.time())

    _modify(root, mutate)
    return token


def _acl_list(values: Sequence[str] | str) -> list[str]:
    if isinstance(values, str):
        values = values.split(",")
    out: list[str] = []
    for v in values:
        v = v.strip()
        if not v:
            continue
        if v != "*":
            validate_user_name(v)
        if v not in out:
            out.append(v)
    return out


def create_repo(root: str, name: str, *, readers: Sequence[str] = ("*",), writers: Sequence[str] = ("*",),
                public: bool = False, allow_force: bool = False) -> str:
    """Create a bare repo under ROOT/repos and register it. Returns the repo directory."""
    validate_repo_name(name)
    server_init(root)
    acl = {
        "readers": _acl_list(readers),
        "writers": _acl_list(writers),
        "public": bool(public),
        "allow_force": bool(allow_force),
    }
    path = repo_dir(root, name)

    def mutate(data: dict) -> None:
        if name in data["repos"]:
            raise errors.MinigitError(f"repository '{name}' already exists")
        if os.path.exists(path):
            raise errors.MinigitError(
                f"cannot create repository '{name}': '{path}' already exists"
                + (" (names that differ only in case share a directory on this filesystem)"
                   if any(n.casefold() == name.casefold() for n in data["repos"]) else ""))
        Repository.init(path, bare=True)
        data["repos"][name] = acl

    _modify(root, mutate)
    return path


def list_server(root: str) -> str:
    """Human-readable listing of users and repos."""
    cfg = ServerConfig(root)
    lines = ["users:"]
    if cfg.users:
        for name in sorted(cfg.users):
            names = ",".join(t[0] for t in user_tokens(cfg.users[name])) or "-"
            lines.append(f"  {name}  tokens={names}")
    else:
        lines.append("  (none)")
    lines.append("repos:")
    if cfg.repos:
        for name in sorted(cfg.repos):
            rec = cfg.repos[name] if isinstance(cfg.repos[name], dict) else {}
            readers = ",".join(rec.get("readers") or []) or "-"
            writers = ",".join(rec.get("writers") or []) or "-"
            flags = []
            if rec.get("public"):
                flags.append("public")
            if rec.get("allow_force"):
                flags.append("allow-force")
            extra = f" [{' '.join(flags)}]" if flags else ""
            lines.append(f"  {name}  readers={readers} writers={writers}{extra}")
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"
